from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr

from cua.models.actions import ClickDecision
from cua.models.bundles import TargetDefinition
from cua.profiles.parabank import LOCATORS
from cua.sessions import ActorStaleEpoch, PrincipalSpec, SessionError, SessionManager
from cua.surface import PlaywrightSurface, SurfaceError
from testbed.seed import seed


@pytest.mark.skipif(
    os.environ.get("CUA_PARABANK_LIVE_TEST") != "1",
    reason="requires the explicitly enabled loopback-only synthetic ParaBank target",
)
def test_native_surface_identity_membership_navigation_and_href_fencing():
    names = {
        "PARABANK_DEMO_ALPHA_USERNAME": "cua_alpha_" + secrets.token_hex(3),
        "PARABANK_DEMO_ALPHA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_BETA_USERNAME": "cua_beta_" + secrets.token_hex(3),
        "PARABANK_DEMO_BETA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_GAMMA_USERNAME": "cua_gamma_" + secrets.token_hex(3),
        "PARABANK_DEMO_GAMMA_PASSWORD": secrets.token_hex(8),
        "PARABANK_DEMO_DELTA_USERNAME": "cua_delta_" + secrets.token_hex(3),
        "PARABANK_DEMO_DELTA_PASSWORD": secrets.token_hex(8),
    }
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    try:
        seed()
        manifest = json.loads(Path("testbed/.cache/seed_manifest.json").read_text(encoding="utf-8"))
        alpha = next(item for item in manifest["principals"] if item["alias"] == "alpha")
        savings = [item["account_id"] for item in alpha["accounts"] if item["type"] == "SAVINGS"]
        if len(savings) < 2:
            raise AssertionError("synthetic principal lacks two savings accounts")
        asyncio.run(_exercise_surface(savings[0], savings[1]))
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def _exercise_surface(account_a: str, account_b: str) -> None:
    manager = SessionManager(
        (
            PrincipalSpec(
                "alpha",
                "PARABANK_DEMO_ALPHA_USERNAME",
                "PARABANK_DEMO_ALPHA_PASSWORD",
                "Synthetic Alpha",
            ),
            PrincipalSpec(
                "wrong",
                "PARABANK_DEMO_ALPHA_USERNAME",
                "PARABANK_DEMO_ALPHA_PASSWORD",
                "Synthetic Other",
            ),
        ),
        headless=True,
    )
    surface = PlaywrightSurface(manager, sample_interval_seconds=0.05)
    handle = None
    actor = None
    run_id = "run_surface_acceptance"

    async def submit(operation):
        return await actor.submit(
            expected_epoch=actor.epoch,
            run_id=run_id,
            operation=operation,
        )

    async def expect_surface_code(code: str, operation) -> None:
        try:
            await operation
        except SurfaceError as error:
            if error.code != code:
                raise AssertionError("surface returned the wrong rejection code") from None
        else:
            raise AssertionError("surface accepted an unsafe or stale target")

    async def wait_path(path: str) -> None:
        session = await submit(lambda: manager.get(handle.session_id))
        await submit(lambda: session.page.wait_for_function(
            "(expected) => location.pathname.endsWith(expected)",
            arg=path,
            timeout=8_000,
        ))

    async def wait_state(route: str, state: str, bindings=None):
        for _ in range(40):
            observation, view = await submit(
                lambda: surface.observe(handle.session_id, bindings=bindings)
            )
            if observation.safe_route == route and view.page_state == state:
                return observation, view
            await asyncio.sleep(0.2)
        raise AssertionError("bounded wait for the native UI state expired")

    try:
        handle = await manager.prepare("alpha")
        if handle.origin != "http://127.0.0.1:8080":
            raise AssertionError("session origin must be authority-only")
        if manager.base_url != "http://127.0.0.1:8080/parabank":
            raise AssertionError("navigation base lost the application context path")
        actor = (await manager.get_state(handle.session_id)).actor
        await actor.begin_run(run_id)
        binding_a = {"inputs.account_id": SecretStr(account_a)}
        binding_b = {"inputs.account_id": SecretStr(account_b)}

        observation, view = await wait_state("accounts_overview", "OVERVIEW_READY", binding_a)
        if view.membership_validity.get("inputs.account_id") is not True:
            raise AssertionError("complete overview did not prove requested account membership")
        same_snapshot = await submit(
            lambda: surface.current_observation(handle.session_id, observation.id)
        )
        if same_snapshot[0].id != observation.id or same_snapshot[1].observation_id != observation.id:
            raise AssertionError("stored snapshot accessor minted a different observation ID")

        page = (await submit(lambda: manager.get(handle.session_id))).page
        await submit(lambda: page.evaluate(
            """() => {
              const body = document.querySelector('#accountTable tbody');
              if (!body || !body.rows.length) return false;
              const last = body.rows[body.rows.length - 1];
              if ((last.cells[0]?.textContent || '').trim() !== 'Total') return false;
              last.remove();
              return true;
            }"""
        ))
        await expect_surface_code(
            "STALE_OBSERVATION",
            submit(lambda: surface.current_observation(handle.session_id, observation.id)),
        )
        await submit(lambda: page.reload(wait_until="domcontentloaded"))
        observation, view = await wait_state("accounts_overview", "OVERVIEW_READY", binding_a)

        await submit(lambda: page.evaluate(
            """() => {
              const links = Array.from(document.querySelectorAll('#accountTable tbody a'));
              if (!links.length) return false;
              let replacement = '999999999999999999';
              while (links.some((link) => link.textContent.trim() === replacement)) replacement += '9';
              links[0].textContent = replacement;
              return true;
            }"""
        ))
        await expect_surface_code(
            "STALE_OBSERVATION",
            submit(lambda: surface.current_observation(handle.session_id, observation.id)),
        )
        await submit(lambda: page.reload(wait_until="domcontentloaded"))
        observation, view = await wait_state("accounts_overview", "OVERVIEW_READY", binding_a)

        unknown_account = TargetDefinition(
            ref="unknown_account",
            locator="TABLE_ACCOUNT_LINK_BY_INPUT",
            allowed_operations=("CLICK",),
            binding_ref="inputs.account_id",
        )
        await expect_surface_code(
            "ACCOUNT_NOT_PRESENT",
            submit(lambda: surface.resolve_target(
                handle.session_id,
                observation.id,
                unknown_account,
                {"inputs.account_id": SecretStr("999999999999999999999999")},
            )),
        )

        nav_control = next(item for item in observation.controls if item.safe_name == "Accounts Overview")
        wrong_frame = ClickDecision(
            observation_id=observation.id,
            operation="CLICK",
            control_ref=nav_control.ref,
            reason_code="NAVIGATE",
            rationale="Reject a control from another frame",
        )
        await expect_surface_code(
            "WRONG_FRAME",
            submit(lambda: surface.resolve_observed(
                handle.session_id,
                wrong_frame,
                frame_ref="f_child",
            )),
        )

        await submit(lambda: page.evaluate(
            """() => {
              const table = document.querySelector('#accountTable tbody');
              if (!table) return false;
              const rows = table.querySelectorAll('tr');
              if (rows.length < 2) return false;
              table.insertBefore(rows[0].cloneNode(true), rows[rows.length - 1]);
              return true;
            }"""
        ))
        await expect_surface_code(
            "TARGET_AMBIGUOUS",
            submit(lambda: surface.observe(handle.session_id, bindings=binding_a)),
        )
        await submit(lambda: page.reload(wait_until="domcontentloaded"))
        observation, view = await wait_state("accounts_overview", "OVERVIEW_READY", binding_a)

        await submit(lambda: page.evaluate(
            """() => {
              const link = Array.from(document.querySelectorAll('a')).find(
                (item) => item.textContent.trim() === 'Accounts Overview'
              );
              if (!link || !link.parentElement) return false;
              link.parentElement.insertBefore(link.cloneNode(true), link.nextSibling);
              return true;
            }"""
        ))
        await expect_surface_code(
            "TARGET_AMBIGUOUS",
            submit(lambda: surface.observe(handle.session_id, bindings=binding_a)),
        )
        await submit(lambda: page.reload(wait_until="domcontentloaded"))
        observation, view = await wait_state("accounts_overview", "OVERVIEW_READY", binding_a)
        proof = view.membership_proof(
            run_ref=run_id,
            binding_ref="inputs.account_id",
            binding_value=binding_a["inputs.account_id"],
        )

        account_target = TargetDefinition(
            ref="requested_account",
            locator="TABLE_ACCOUNT_LINK_BY_INPUT",
            allowed_operations=("CLICK",),
            binding_ref="inputs.account_id",
        )
        resolved = await submit(lambda: surface.resolve_target(
            handle.session_id,
            observation.id,
            account_target,
            binding_a,
        ))
        requested_control = next(
            item for item in observation.controls if item.safe_name == "<requested_account>"
        )
        observed_decision = ClickDecision(
            observation_id=observation.id,
            operation="CLICK",
            control_ref=requested_control.ref,
            reason_code="OPEN_ACCOUNT",
            rationale="Resolve the observed requested account control",
        )
        observed_resolved = await submit(lambda: surface.resolve_observed(
            handle.session_id,
            observed_decision,
        ))
        if (
            observed_resolved.binding_value is None
            or observed_resolved.binding_value.get_secret_value() != account_a
        ):
            raise AssertionError("observed account target lost its protected input binding")
        if (
            resolved.destination_origin,
            resolved.destination_route,
            resolved.runtime_risk,
        ) != ("http://127.0.0.1:8080", "account_details", "READ_ONLY"):
            raise AssertionError("actual account-link destination was not safely classified")
        decision = ClickDecision(
            observation_id=observation.id,
            operation="CLICK",
            control_ref=resolved.control_ref,
            reason_code="OPEN_ACCOUNT",
            rationale="Open the requested account",
        )
        account_locator = page.locator(LOCATORS["TABLE_ACCOUNT_LINK_BY_INPUT"]["scope"]).get_by_role(
            LOCATORS["TABLE_ACCOUNT_LINK_BY_INPUT"]["role"],
            name=account_a,
            exact=LOCATORS["TABLE_ACCOUNT_LINK_BY_INPUT"]["exact"],
        )
        original_href = await submit(lambda: account_locator.get_attribute("href"))
        if not original_href:
            raise AssertionError("native account link lacks a same-origin destination")
        try:
            await submit(lambda: account_locator.evaluate(
                "(element) => element.setAttribute('href', 'https://example.invalid/transfer.htm')"
            ))
            await expect_surface_code(
                "UNSAFE_DESTINATION",
                submit(lambda: surface.execute(handle.session_id, decision, resolved)),
            )
            current_path = await submit(lambda: page.evaluate("() => location.pathname"))
            if not current_path.endswith("/overview.htm"):
                raise AssertionError("external href tampering caused navigation")
        finally:
            await submit(lambda: account_locator.evaluate(
                "(element, href) => element.setAttribute('href', href)",
                original_href,
            ))

        await submit(lambda: surface.execute(handle.session_id, decision, resolved))
        await wait_path("/activity.htm")
        unbound_observation, unbound_detail = await wait_state("account_details", "DETAIL_READY")
        if unbound_detail.detail_identity_match is not None:
            raise AssertionError("detail readiness still depends on adapter click history")
        try:
            unbound_detail.completion_view(run_ref=run_id)
        except SurfaceError as error:
            if error.code != "COMPLETION_VIEW_UNAVAILABLE":
                raise AssertionError("unbound detail read incorrectly passed completion") from None
        else:
            raise AssertionError("completion passed without a requested account binding")

        await submit(lambda: page.evaluate(
            """() => {
              const balance = document.querySelector('#balance');
              const available = document.querySelector('#availableBalance');
              if (!balance || !available) return false;
              balance.id = 'temporaryBalanceField';
              available.id = 'balance';
              balance.id = 'availableBalance';
              return true;
            }"""
        ))
        try:
            await expect_surface_code(
                "FIELD_RELATION_UNVERIFIED",
                submit(lambda: surface.observe(handle.session_id, bindings=binding_a)),
            )
        finally:
            await submit(lambda: page.evaluate(
                """() => {
                  const balance = document.querySelector('#availableBalance');
                  const available = document.querySelector('#balance');
                  if (!balance || !available) return false;
                  balance.id = 'temporaryBalanceField';
                  available.id = 'availableBalance';
                  balance.id = 'balance';
                  return true;
                }"""
            ))

        await expect_surface_code(
            "SUBJECT_MISMATCH",
            submit(lambda: surface.observe(handle.session_id, bindings=binding_b)),
        )
        detail_observation, detail_view = await wait_state(
            "account_details", "DETAIL_READY", binding_a
        )
        completion = detail_view.completion_view(run_ref=run_id)
        if detail_view.detail_identity_match is not True:
            raise AssertionError("requested detail account was not bound")
        if proof.account_binding_value.get_secret_value() != completion.account_number_values[0].get_secret_value():
            raise AssertionError("completion account differs from the complete overview proof")

        await submit(lambda: page.evaluate(
            """() => {
              const available = document.querySelector('#availableBalance');
              if (!available) return false;
              available.textContent = '$7.77';
              return true;
            }"""
        ))
        await expect_surface_code(
            "STALE_OBSERVATION",
            submit(lambda: surface.current_observation(handle.session_id, detail_observation.id)),
        )
        await submit(lambda: page.reload(wait_until="domcontentloaded"))
        detail_observation, detail_view = await wait_state(
            "account_details", "DETAIL_READY", binding_a
        )
        completion = detail_view.completion_view(run_ref=run_id)

        overview_target = TargetDefinition(
            ref="overview_nav",
            locator="ROLE_LINK_ACCOUNTS_OVERVIEW",
            allowed_operations=("CLICK",),
        )
        resolved_nav = await submit(lambda: surface.resolve_target(
            handle.session_id,
            detail_observation.id,
            overview_target,
            {},
        ))
        if (
            resolved_nav.destination_origin,
            resolved_nav.destination_route,
            resolved_nav.runtime_risk,
        ) != ("http://127.0.0.1:8080", "accounts_overview", "READ_ONLY"):
            raise AssertionError("actual overview-link destination was not safely classified")
        nav_decision = ClickDecision(
            observation_id=detail_observation.id,
            operation="CLICK",
            control_ref=resolved_nav.control_ref,
            reason_code="NAVIGATE",
            rationale="Return to the account overview",
        )
        await submit(lambda: surface.execute(handle.session_id, nav_decision, resolved_nav))
        await wait_path("/overview.htm")
        await expect_surface_code(
            "STALE_OBSERVATION",
            submit(lambda: surface.current_observation(handle.session_id, detail_observation.id)),
        )
        observation, view = await wait_state("accounts_overview", "OVERVIEW_READY", binding_a)

        async def assert_terminal_snapshot(expected_state: str, *, blocked: bool = False) -> None:
            terminal_observation, terminal_view = await submit(
                lambda: surface.observe(handle.session_id, bindings=binding_a)
            )
            if terminal_view.page_state != expected_state:
                raise AssertionError("visible terminal signal produced the wrong page state")
            if (
                terminal_observation.controls
                or terminal_observation.safe_text
                or terminal_view.field_values
                or terminal_view.account_ids
                or terminal_view.membership_validity
            ):
                raise AssertionError("terminal snapshot exposed protected fields or actions")
            if terminal_view.parseable_fields or terminal_view.unknown_blocker != blocked:
                raise AssertionError("terminal snapshot retained protected readiness evidence")

        async def inject_generic_error(*, make_incomplete: bool) -> None:
            installed = await submit(lambda: page.evaluate(
                """(incomplete) => {
                  const body = document.querySelector('#accountTable tbody');
                  if (!body) return false;
                  const rows = body.querySelectorAll('tr');
                  if (!rows.length) return false;
                  const cell = rows[rows.length - 1].querySelector('td, th');
                  if (!cell) return false;
                  if (incomplete) {
                    cell.setAttribute('data-cua-test-original-text', cell.textContent || '');
                    cell.textContent = 'Synthetic incomplete marker';
                  }
                  const error = document.createElement('p');
                  error.className = 'error';
                  error.setAttribute('data-cua-test-signal', 'generic-error');
                  error.textContent = 'Synthetic application error';
                  document.body.append(error);
                  return true;
                }""",
                make_incomplete,
            ))
            if not installed:
                raise AssertionError("test-only generic error injection failed")

        async def clear_generic_error(*, restore_total: bool) -> None:
            await submit(lambda: page.evaluate(
                """(restore) => {
                  document.querySelector('[data-cua-test-signal="generic-error"]')?.remove();
                  if (restore) {
                    const cell = document.querySelector('[data-cua-test-original-text]');
                    if (cell) {
                      cell.textContent = cell.getAttribute('data-cua-test-original-text') || '';
                      cell.removeAttribute('data-cua-test-original-text');
                    }
                  }
                  return true;
                }""",
                restore_total,
            ))

        # Contradictory error + otherwise-ready overview must fail closed.
        await inject_generic_error(make_incomplete=False)
        await expect_surface_code(
            "AMBIGUOUS_STATE",
            submit(lambda: surface.observe(handle.session_id, bindings=binding_a)),
        )
        await clear_generic_error(restore_total=False)

        # A generic visible `.error` without a complete ready route is APP_ERROR, never denial.
        await inject_generic_error(make_incomplete=True)
        await assert_terminal_snapshot("APP_ERROR")
        await clear_generic_error(restore_total=True)

        # Test-only exact accessible signal exercises the explicit-denial branch.
        await submit(lambda: page.evaluate(
            """() => {
              const body = document.querySelector('#accountTable tbody');
              if (!body || !body.rows.length) return false;
              const cell = body.rows[body.rows.length - 1].querySelector('td, th');
              if (!cell) return false;
              cell.setAttribute('data-cua-test-original-text', cell.textContent || '');
              cell.textContent = 'Synthetic incomplete marker';
              const alert = document.createElement('div');
              alert.setAttribute('role', 'alert');
              alert.setAttribute('data-cua-test-signal', 'explicit-denial');
              alert.textContent = 'Access Denied';
              document.body.append(alert);
              return true;
            }"""
        ))
        await assert_terminal_snapshot("ACCESS_DENIED")
        await submit(lambda: page.evaluate(
            """() => {
              document.querySelector('[data-cua-test-signal="explicit-denial"]')?.remove();
              const cell = document.querySelector('[data-cua-test-original-text]');
              if (cell) {
                cell.textContent = cell.getAttribute('data-cua-test-original-text') || '';
                cell.removeAttribute('data-cua-test-original-text');
              }
              return true;
            }"""
        ))
        observation, view = await wait_state("accounts_overview", "OVERVIEW_READY", binding_a)

        # Navigate to a real detail page and inject a generic dialog over visible fields.
        account_target = TargetDefinition(
            ref="requested_account",
            locator="TABLE_ACCOUNT_LINK_BY_INPUT",
            allowed_operations=("CLICK",),
            binding_ref="inputs.account_id",
        )
        requested_link = next(item for item in observation.controls if item.safe_name == "<requested_account>")
        detail_decision = ClickDecision(
            observation_id=observation.id,
            operation="CLICK",
            control_ref=requested_link.ref,
            reason_code="OPEN_ACCOUNT",
            rationale="Open the requested synthetic account",
        )
        detail_resolved = await submit(lambda: surface.resolve_observed(
            handle.session_id,
            detail_decision,
        ))
        if detail_resolved.binding_value is None:
            raise AssertionError("observed account link lost its input binding")
        await submit(lambda: surface.execute(handle.session_id, detail_decision, detail_resolved))
        await wait_path("/activity.htm")
        await wait_state("account_details", "DETAIL_READY", binding_a)
        await submit(lambda: page.evaluate(
            """() => {
              const dialog = document.createElement('div');
              dialog.setAttribute('role', 'dialog');
              dialog.setAttribute('aria-modal', 'true');
              dialog.setAttribute('data-cua-test-signal', 'unknown-modal');
              dialog.textContent = 'Synthetic modal blocker';
              dialog.style.cssText = 'position:fixed;inset:10px;z-index:2147483647;background:white;display:block';
              document.body.append(dialog);
              return true;
            }"""
        ))
        await assert_terminal_snapshot("UNKNOWN", blocked=True)
        await submit(lambda: page.evaluate(
            "() => document.querySelector('[data-cua-test-signal=\"unknown-modal\"]')?.remove()"
        ))
        await wait_state("account_details", "DETAIL_READY", binding_a)
        observation = await submit(lambda: surface.observe(handle.session_id, bindings=binding_a))
        if isinstance(observation, tuple):
            observation = observation[0]
        overview_target = TargetDefinition(
            ref="overview_nav",
            locator="ROLE_LINK_ACCOUNTS_OVERVIEW",
            allowed_operations=("CLICK",),
        )
        overview_resolved = await submit(lambda: surface.resolve_target(
            handle.session_id,
            observation.id,
            overview_target,
            {},
        ))
        overview_decision = ClickDecision(
            observation_id=observation.id,
            operation="CLICK",
            control_ref=overview_resolved.control_ref,
            reason_code="NAVIGATE",
            rationale="Return to the synthetic account overview",
        )
        await submit(lambda: surface.execute(handle.session_id, overview_decision, overview_resolved))
        await wait_path("/overview.htm")

        try:
            await manager.prepare("wrong")
        except SessionError as error:
            if error.code != "SUBJECT_MISMATCH" or error.session_id is None:
                raise AssertionError("mismatched visible identity was not rejected explicitly") from None
            if (await manager.get_state(error.session_id)).state != "SUBJECT_MISMATCH":
                raise AssertionError("mismatched session context was not preserved for intervention")
        else:
            raise AssertionError("mismatched visible identity unexpectedly authenticated")

        managed = manager._sessions[handle.session_id]
        original_context, original_page = managed.context, managed.page
        old_page_id = handle.page_id
        old_auth_generation = handle.auth_generation
        pausing = await actor.pause_and_drain(expected_epoch=actor.epoch)
        human = await actor.claim_human(expected_epoch=pausing.epoch)
        logout = original_page.get_by_role("link", name="Log Out", exact=True)
        if await logout.count() != 1 or not await logout.is_visible():
            raise AssertionError("pinned UI does not expose the expected logout link")
        await logout.click()
        await original_page.locator(LOCATORS["LOGIN_USERNAME"]["selector"]).wait_for(
            state="visible",
            timeout=8_000,
        )
        resuming = await actor.begin_resume(expected_epoch=human.epoch)
        try:
            await manager.reauthenticate_existing(
                handle.session_id,
                expected_epoch=human.epoch,
            )
        except ActorStaleEpoch:
            pass
        else:
            raise AssertionError("reauthentication accepted the pre-resume ownership epoch")
        returned = await manager.reauthenticate_existing(
            handle.session_id,
            expected_epoch=resuming.epoch,
        )
        if managed.context is not original_context or managed.page is not original_page:
            raise AssertionError("reauthentication replaced the existing context or page")
        if returned.page_id != old_page_id or returned.auth_generation <= old_auth_generation:
            raise AssertionError("same-context reauthentication did not advance auth generation")
        if actor.snapshot.owner != "RESUMING" or actor.snapshot.epoch != resuming.epoch:
            raise AssertionError("reauthentication changed ownership before postcondition review")
    finally:
        if actor is not None and actor.active_run_id == run_id:
            await actor.finish_run(run_id)
        await manager.close_all()
