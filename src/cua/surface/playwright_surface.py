"""Async, profile-driven Playwright observations and generation-bound clicks."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import hmac
import json
import secrets
import time
from types import MappingProxyType
from typing import Literal, Mapping
from urllib.parse import parse_qsl, urljoin, urlsplit

from pydantic import SecretStr
from playwright.async_api import ElementHandle, Locator, Page, Error as PlaywrightError

from cua.conditions.evaluator import ConditionContext
from cua.conditions.parsers import AmountParseError, USDDecimalParser
from cua.models.actions import ClickDecision, Decision
from cua.models.bundles import TargetDefinition
from cua.models.observations import Observation, ObservedControl
from cua.models.verification import CompletionView, MembershipProof
from cua.profiles.parabank import (
    ERROR_SIGNAL_RULES,
    EXPLICIT_DENIAL_SIGNALS,
    LOCATORS,
    PAGE_STATES,
    PROFILE_ID,
    READINESS_RULES,
    ROUTES,
)
from cua.sessions.manager import ManagedSession, SessionError, SessionManager


class SurfaceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NormalizedView:
    """Ephemeral semantic values; page data stays protected and in memory."""

    observation_id: str
    session_id: str
    authentication_generation: int
    profile_id: str
    origin: str
    safe_route: str
    page_state: str
    principal_matches: bool | None
    overview_complete: bool | None
    membership_validity: Mapping[str, bool | None] = field(repr=False)
    account_ids: frozenset[str] = field(default_factory=frozenset, repr=False)
    field_values: Mapping[str, tuple[SecretStr, ...]] = field(default_factory=dict, repr=False)
    field_relations: Mapping[str, bool] = field(default_factory=dict, repr=False)
    parseable_fields: frozenset[str] = field(default_factory=frozenset, repr=False)
    detail_identity_match: bool | None = None
    unknown_blocker: bool = False
    captured_monotonic_ms: int = 0
    parsed_amounts: Mapping[str, Decimal] = field(default_factory=dict, repr=False)

    def condition_context(self, input_values: Mapping[str, SecretStr] | None = None) -> ConditionContext:
        inputs = dict(input_values or {})
        account_presence: dict[str, bool | None] = {}
        membership = dict(self.membership_validity)
        account_input = inputs.get("inputs.account_id") or inputs.get("account_id")
        if account_input is not None:
            account_id = account_input.get_secret_value()
            account_presence["inputs.account_id"] = account_id in self.account_ids
            membership["inputs.account_id"] = (
                self.principal_matches is True
                and self.overview_complete is True
                and account_id in self.account_ids
            )
        return ConditionContext(
            page_state=self.page_state,
            principal_matches=self.principal_matches,
            overview_complete=self.overview_complete,
            account_presence=MappingProxyType(account_presence),
            membership_validity=MappingProxyType(membership),
            field_values=self.field_values,
            input_values=MappingProxyType(inputs),
        )

    def membership_proof(
        self,
        *,
        run_ref: str,
        binding_ref: str,
        binding_value: SecretStr,
    ) -> MembershipProof:
        value = binding_value.get_secret_value()
        if (
            self.safe_route != "accounts_overview"
            or self.page_state != "OVERVIEW_READY"
            or self.overview_complete is not True
            or self.principal_matches is not True
            or binding_ref != "inputs.account_id"
            or value not in self.account_ids
            or self.membership_validity.get(binding_ref) is False
        ):
            raise SurfaceError("MEMBERSHIP_PROOF_UNAVAILABLE")
        return MembershipProof(
            proof_ref="proof_" + secrets.token_hex(8),
            run_ref=run_ref,
            session_ref=self.session_id,
            authentication_generation=self.authentication_generation,
            account_binding_ref="inputs.account_id",
            account_binding_value=binding_value,
            overview_observation_ref=self.observation_id,
            overview_complete=True,
            account_present=True,
            verified_monotonic_ms=self.captured_monotonic_ms,
        )

    def completion_view(self, *, run_ref: str) -> CompletionView:
        required = (
            "PROFILE_ACCOUNT_NUMBER",
            "PROFILE_ACCOUNT_TYPE",
            "PROFILE_AVAILABLE_BALANCE",
        )
        if (
            self.safe_route != "account_details"
            or self.page_state != "DETAIL_READY"
            or self.principal_matches is not True
            or self.detail_identity_match is not True
            or not all(self.field_relations.get(key) is True for key in required)
            or "PROFILE_AVAILABLE_BALANCE" not in self.parseable_fields
        ):
            raise SurfaceError("COMPLETION_VIEW_UNAVAILABLE")
        return CompletionView(
            run_ref=run_ref,
            session_ref=self.session_id,
            observation_ref=self.observation_id,
            authentication_generation=self.authentication_generation,
            origin=self.origin,
            profile_id=PROFILE_ID,
            safe_route="account_details",
            account_number_field_ref="PROFILE_ACCOUNT_NUMBER",
            account_type_field_ref="PROFILE_ACCOUNT_TYPE",
            available_balance_field_ref="PROFILE_AVAILABLE_BALANCE",
            available_balance_parser_id=USDDecimalParser.parser_id,
            available_balance_parser_version=USDDecimalParser.version,
            available_balance_currency="USD",
            page_state=self.page_state,
            principal_matches=self.principal_matches,
            account_number_values=self.field_values["PROFILE_ACCOUNT_NUMBER"],
            account_type_values=self.field_values["PROFILE_ACCOUNT_TYPE"],
            available_balance_values=self.field_values["PROFILE_AVAILABLE_BALANCE"],
        )


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    target_ref: str
    locator_key: str
    allowed_operations: tuple[str, ...]
    binding_ref: str | None
    observation_id: str
    control_ref: str | None
    frame_ref: str
    page_id: str
    document_generation: int
    authentication_generation: int
    session_id: str
    destination_origin: str
    destination_route: str
    runtime_risk: Literal["READ_ONLY", "WRITE", "UNKNOWN"]
    unique: bool
    visible: bool
    enabled: bool
    token: str = field(repr=False)
    binding_value: SecretStr | None = field(default=None, repr=False)


@dataclass(slots=True, repr=False)
class _BoundControl:
    locator_key: str
    element: ElementHandle | None = field(repr=False)
    safe_name: str
    frame_ref: str = "f_main"
    binding_ref: str | None = None
    binding_value: str | None = field(default=None, repr=False)


@dataclass(slots=True, repr=False)
class _Record:
    observation: Observation
    view: NormalizedView
    controls: dict[str, _BoundControl] = field(repr=False)
    document_generation: int
    authentication_generation: int
    semantic_fingerprint: bytes = field(repr=False)


class PlaywrightSurface:
    def __init__(self, sessions: SessionManager, *, sample_interval_seconds: float = 0.10) -> None:
        self._sessions = sessions
        self._sample_interval = sample_interval_seconds
        self._latest: dict[str, _Record] = {}
        self._resolved: dict[str, tuple[_Record, _BoundControl | None]] = {}
        self._freshness_key = secrets.token_bytes(32)

    async def current_observation(
        self,
        session_id: str,
        expected_observation_id: str,
    ) -> tuple[Observation, NormalizedView]:
        """Return the stored snapshot without minting a new observation ID."""
        record, _ = await self._current(session_id, expected_observation_id)
        return record.observation, record.view

    async def observe(
        self,
        session_id: str,
        *,
        bindings: Mapping[str, SecretStr] | None = None,
    ) -> tuple[Observation, NormalizedView]:
        session = await self._active(session_id)
        await self._discard(session_id)
        route = _route_name(session.page)
        captured = time.monotonic_ns() // 1_000_000
        controls: dict[str, _BoundControl] = {}
        fields: dict[str, tuple[SecretStr, ...]] = {}
        relations: dict[str, bool] = {}
        parsed: dict[str, Decimal] = {}
        account_ids: frozenset[str] = frozenset()
        membership: dict[str, bool | None] = {}
        overview_complete: bool | None = None
        detail_match: bool | None = None
        safe_text: tuple[str, ...] = ()
        state = "UNKNOWN"
        terminal_state, unknown_blocker = await _terminal_page_state(session.page, route)

        if terminal_state is not None:
            state = terminal_state
        elif route == "home":
            state, controls, safe_text = await self._observe_home(session)
        elif route == "accounts_overview":
            state, controls, account_ids, overview_complete, safe_text = await self._observe_overview(session, bindings)
            if state in {"OVERVIEW_READY", "OVERVIEW_LOADING"}:
                fields["overview.account_ids"] = tuple(SecretStr(value) for value in sorted(account_ids))
                account_input = _binding_value(bindings, "inputs.account_id")
                if account_input is not None:
                    membership["inputs.account_id"] = bool(
                        overview_complete is True and account_input in account_ids
                    )
        elif route == "account_details":
            state, safe_text, fields, relations, detail_match, parsed = await self._observe_details(session, bindings)
            if state == "DETAIL_READY":
                nav = await _optional_overview_nav(session)
                if nav is not None:
                    ref, control = nav
                    controls[ref] = control
        if terminal_state is None:
            final_terminal, final_blocker = await _terminal_page_state(session.page, route)
            if final_terminal is not None:
                for control in controls.values():
                    await _dispose(control.element)
                state = final_terminal
                unknown_blocker = final_blocker
                controls = {}
                fields = {}
                relations = {}
                parsed = {}
                account_ids = frozenset()
                membership = {}
                overview_complete = None
                detail_match = None
                safe_text = ()
        if state not in PAGE_STATES:
            state = "UNKNOWN"

        observation_id = "o_" + secrets.token_hex(12)
        observation = Observation(
            id=observation_id,
            session_id=session.handle.session_id,
            page_id=session.handle.page_id,
            document_generation=session.document_generation,
            frame_generations=dict(session.frame_generations),
            captured_monotonic_ms=captured,
            safe_route=route,
            state_tags=(state,),
            controls=tuple(_public_control(ref, control) for ref, control in controls.items()),
            safe_text=safe_text,
            fingerprint=_observation_fingerprint(route, state, controls),
        )
        view = NormalizedView(
            observation_id=observation_id,
            session_id=session.handle.session_id,
            authentication_generation=session.handle.auth_generation,
            profile_id=session.handle.profile_id,
            origin=session.handle.origin,
            safe_route=route,
            page_state=state,
            principal_matches=True,
            overview_complete=overview_complete,
            membership_validity=MappingProxyType(membership),
            account_ids=account_ids,
            field_values=MappingProxyType(fields),
            field_relations=MappingProxyType(relations),
            parseable_fields=frozenset(parsed),
            detail_identity_match=detail_match,
            unknown_blocker=unknown_blocker,
            captured_monotonic_ms=captured,
            parsed_amounts=MappingProxyType(parsed),
        )
        semantic_fingerprint = self._semantic_fingerprint(
            route=route,
            state=state,
            overview_complete=overview_complete,
            account_ids=account_ids,
            field_values=fields,
            field_relations=relations,
            parsed_amounts=parsed,
            controls=controls,
            page_url=session.page.url,
            unknown_blocker=unknown_blocker,
        )
        self._latest[session_id] = _Record(
            observation=observation,
            view=view,
            controls=controls,
            document_generation=session.document_generation,
            authentication_generation=session.handle.auth_generation,
            semantic_fingerprint=semantic_fingerprint,
        )
        return observation, view

    async def resolve_target(
        self,
        session_id: str,
        observation_id: str,
        target: TargetDefinition,
        bindings: Mapping[str, SecretStr],
    ) -> ResolvedTarget:
        record, session = await self._current(session_id, observation_id)
        if target.locator == "ROLE_LINK_ACCOUNTS_OVERVIEW":
            if record.observation.safe_route not in {"home", "accounts_overview", "account_details"} or "CLICK" not in target.allowed_operations:
                raise SurfaceError("TARGET_NOT_AVAILABLE")
            candidates = [(ref, item) for ref, item in record.controls.items() if item.locator_key == target.locator]
            ref, bound = _single(candidates)
            return await self._resolve(session, record, target, ref, bound)
        if target.locator == "TABLE_ACCOUNT_LINK_BY_INPUT":
            if record.observation.safe_route != "accounts_overview" or target.binding_ref != "inputs.account_id":
                raise SurfaceError("TARGET_BINDING_MISMATCH")
            account_id = _binding_value(bindings, target.binding_ref)
            if account_id is None:
                raise SurfaceError("TARGET_BINDING_MISSING")
            if record.view.overview_complete is not True or account_id not in record.view.account_ids:
                raise SurfaceError("ACCOUNT_NOT_PRESENT")
            if record.view.membership_validity.get(target.binding_ref) is False:
                raise SurfaceError("ACCOUNT_NOT_PRESENT")
            candidates = [
                (ref, item)
                for ref, item in record.controls.items()
                if item.locator_key == target.locator and item.binding_value == account_id
            ]
            ref, bound = _single(candidates)
            return await self._resolve(
                session, record, target, ref, bound, binding_value=bindings[target.binding_ref]
            )
        if target.locator in {
            "PROFILE_ACCOUNT_NUMBER",
            "PROFILE_ACCOUNT_TYPE",
            "PROFILE_AVAILABLE_BALANCE",
        }:
            if record.observation.safe_route != "account_details" or "READ" not in target.allowed_operations:
                raise SurfaceError("TARGET_NOT_AVAILABLE")
            if record.view.field_relations.get(target.locator) is not True:
                raise SurfaceError("FIELD_RELATION_UNVERIFIED")
            locator = _profile_locator(session.page, target.locator)
            await _require_unique_visible(locator)
            return await self._resolve(session, record, target, None, None)
        raise SurfaceError("UNSUPPORTED_PROFILE_TARGET")

    async def resolve_observed(
        self,
        session_id: str,
        decision: ClickDecision,
        *,
        frame_ref: str = "f_main",
    ) -> ResolvedTarget:
        record, session = await self._current(session_id, decision.observation_id)
        if frame_ref != "f_main":
            raise SurfaceError("WRONG_FRAME")
        bound = record.controls.get(decision.control_ref)
        if bound is None:
            raise SurfaceError("UNKNOWN_CONTROL")
        if bound.frame_ref != frame_ref:
            raise SurfaceError("WRONG_FRAME")
        target = TargetDefinition(
            ref="observed_target",
            locator=bound.locator_key,
            allowed_operations=("CLICK",),
            binding_ref=bound.binding_ref,
        )
        return await self._resolve(session, record, target, decision.control_ref, bound)

    async def execute(
        self,
        session_id: str,
        decision: Decision,
        resolved: ResolvedTarget,
    ) -> None:
        if not isinstance(decision, ClickDecision) or decision.operation != "CLICK":
            raise SurfaceError("UNSUPPORTED_ACTION")
        if "CLICK" not in resolved.allowed_operations or resolved.control_ref is None:
            raise SurfaceError("ACTION_NOT_ALLOWED")
        if (
            resolved.session_id != session_id
            or resolved.observation_id != decision.observation_id
            or resolved.control_ref != decision.control_ref
        ):
            raise SurfaceError("ACTION_BINDING_MISMATCH")
        record, session = await self._current(session_id, decision.observation_id)
        stored = self._resolved.get(resolved.token)
        if stored is None or stored[0] is not record:
            raise SurfaceError("STALE_TARGET")
        bound = stored[1]
        if bound is None or bound.element is None or resolved.frame_ref != "f_main":
            raise SurfaceError("WRONG_FRAME")
        locator = _profile_locator(session.page, bound.locator_key, bound.binding_value)
        await _require_unique_visible(locator)
        current = await locator.element_handle()
        if current is None:
            raise SurfaceError("STALE_TARGET")
        try:
            connected = await bound.element.evaluate("(el) => el.isConnected")
            identical = await bound.element.evaluate("(old, fresh) => old === fresh", current)
            if not connected or not identical:
                raise SurfaceError("STALE_TARGET")
            if not await bound.element.is_visible() or not await bound.element.is_enabled():
                raise SurfaceError("TARGET_NOT_ACTIONABLE")
            destination = await self._classify_destination(session, bound)
            if destination != (
                resolved.destination_origin,
                resolved.destination_route,
                resolved.runtime_risk,
            ) or resolved.runtime_risk != "READ_ONLY":
                raise SurfaceError("UNSAFE_DESTINATION")
            await bound.element.click(timeout=5_000)
        except SurfaceError:
            raise
        except PlaywrightError:
            raise SurfaceError("TARGET_ACTION_FAILED") from None
        finally:
            await _dispose(current)
        await self._discard(session_id)

    async def _observe_home(self, session: ManagedSession):
        rules = READINESS_RULES["AUTHENTICATED_HOME"]
        if "visible_nav_link:Accounts Overview" not in rules or "login_form_not_visible" not in rules:
            raise SurfaceError("PROFILE_HOME_RULES_MISSING")
        ref, control = await _required_overview_nav(session)
        return "AUTHENTICATED_HOME", {ref: control}, ("Welcome",)

    async def _observe_overview(self, session: ManagedSession, bindings):
        rules = READINESS_RULES["OVERVIEW_READY"]
        required = (
            "heading:Accounts Overview",
            "table:#accountTable",
            "visible_last_tbody_row:first-cell-exact-Total",
            "no_visible_loading_or_error",
            "same_account_set_in_two_adjacent_samples",
        )
        if not all(rule in rules for rule in required):
            raise SurfaceError("PROFILE_OVERVIEW_RULES_MISSING")
        heading = session.page.get_by_role("heading", name="Accounts Overview", exact=True)
        table = session.page.locator(LOCATORS["TABLE_ACCOUNT_LINK_BY_INPUT"]["scope"])
        controls: dict[str, _BoundControl] = {}
        try:
            await _require_unique_visible(heading)
            await _require_unique_visible(table)
            first = await _sample_overview(session.page, table)
            await __import__("asyncio").sleep(self._sample_interval)
            second = await _sample_overview(session.page, table)
        except SurfaceError as error:
            if error.code == "TARGET_AMBIGUOUS":
                raise
            nav = await _optional_overview_nav(session)
            if nav is not None:
                controls[nav[0]] = nav[1]
            return "OVERVIEW_LOADING", controls, frozenset(), False, ("Accounts Overview",)
        except PlaywrightError:
            nav = await _optional_overview_nav(session)
            if nav is not None:
                controls[nav[0]] = nav[1]
            return "OVERVIEW_LOADING", controls, frozenset(), False, ("Accounts Overview",)
        terminal_state, _ = await _terminal_page_state(session.page, "accounts_overview")
        if terminal_state is not None:
            for control in controls.values():
                await _dispose(control.element)
            return terminal_state, {}, frozenset(), False, ()
        ids, complete = second
        stable = frozenset(first[0]) == frozenset(second[0])
        complete = bool(complete and stable)
        account_ids = frozenset(ids)
        nav = await _optional_overview_nav(session)
        if nav is not None:
            controls[nav[0]] = nav[1]
        if not complete:
            return "OVERVIEW_LOADING", controls, account_ids, False, ("Accounts Overview",)
        requested = _binding_value(bindings, "inputs.account_id")
        for ordinal, account_id in enumerate(ids, 1):
            locator = _profile_locator(session.page, "TABLE_ACCOUNT_LINK_BY_INPUT", account_id)
            try:
                await _require_unique_visible(locator)
                element = await _element(locator)
            except SurfaceError as error:
                if error.code == "TARGET_AMBIGUOUS":
                    raise
                return "OVERVIEW_LOADING", controls, account_ids, False, ("Accounts Overview",)
            except PlaywrightError:
                return "OVERVIEW_LOADING", controls, account_ids, False, ("Accounts Overview",)
            name = "<requested_account>" if account_id == requested else f"Account {ordinal}"
            ref = _control_ref()
            controls[ref] = _BoundControl(
                "TABLE_ACCOUNT_LINK_BY_INPUT",
                element,
                name,
                binding_ref="inputs.account_id",
                binding_value=account_id,
            )
        return "OVERVIEW_READY", controls, account_ids, True, ("Accounts Overview",)

    async def _observe_details(self, session: ManagedSession, bindings):
        rules = READINESS_RULES["DETAIL_READY"]
        detail_table = LOCATORS["PROFILE_DETAIL_TABLE"]
        field_labels = tuple(
            LOCATORS[key]["label"]
            for key in (
                "PROFILE_ACCOUNT_NUMBER",
                "PROFILE_ACCOUNT_TYPE",
                "PROFILE_AVAILABLE_BALANCE",
            )
        )
        required = (
            "heading:Account Details",
            f"table:{detail_table['selector']}",
            *(f"unique_visible_field:{label}" for label in field_labels),
            f"available_balance:{USDDecimalParser.parser_id}@{USDDecimalParser.version}_parseable",
            "no_visible_error",
        )
        if not all(rule in rules for rule in required):
            raise SurfaceError("PROFILE_DETAIL_RULES_MISSING")
        try:
            if detail_table.get("structural_only") is not True:
                raise SurfaceError("PROFILE_DETAIL_TABLE_NOT_STRUCTURAL")
            await _require_unique_visible(session.page.locator(detail_table["selector"]))
            heading = session.page.get_by_role("heading", name="Account Details", exact=True)
            await _require_unique_visible(heading)
            fields: dict[str, tuple[SecretStr, ...]] = {}
            relations: dict[str, bool] = {}
            values: dict[str, str] = {}
            for key in (
                "PROFILE_ACCOUNT_NUMBER",
                "PROFILE_ACCOUNT_TYPE",
                "PROFILE_AVAILABLE_BALANCE",
            ):
                definition = LOCATORS[key]
                locator = session.page.locator(definition["selector"])
                if definition.get("unique") is not True:
                    raise SurfaceError("PROFILE_FIELD_UNIQUENESS_MISSING")
                await _require_unique_visible(locator)
                value = await _read_value(locator)
                if not value:
                    return "UNKNOWN", ("Account Details",), {}, {}, None, {}
                values[key] = value
                fields[key] = (SecretStr(value),)
                relations[key] = await _label_related(locator, definition["label"])
            if not all(relations.values()):
                raise SurfaceError("FIELD_RELATION_UNVERIFIED")
            terminal_state, _ = await _terminal_page_state(session.page, "account_details")
            if terminal_state is not None:
                return terminal_state, (), {}, {}, None, {}
            parsed = USDDecimalParser().parse(values["PROFILE_AVAILABLE_BALANCE"])
            expected = _binding_value(bindings, "inputs.account_id")
            detail_matches = None if expected is None else values["PROFILE_ACCOUNT_NUMBER"] == expected
            if detail_matches is False:
                raise SurfaceError("SUBJECT_MISMATCH")
            for key, alias in (
                ("PROFILE_ACCOUNT_NUMBER", "detail.account_number"),
                ("PROFILE_ACCOUNT_TYPE", "detail.account_type"),
                ("PROFILE_AVAILABLE_BALANCE", "detail.available_balance"),
            ):
                fields[alias] = fields[key]
            state = "DETAIL_READY"
            return (
                state,
                ("Account Details", *field_labels),
                fields,
                relations,
                detail_matches,
                {"PROFILE_AVAILABLE_BALANCE": parsed},
            )
        except AmountParseError:
            raise SurfaceError("DETAIL_AMOUNT_UNPARSEABLE") from None
        except SurfaceError as error:
            if error.code in {"TARGET_NOT_FOUND", "TARGET_NOT_VISIBLE"}:
                return "UNKNOWN", ("Account Details",), {}, {}, None, {}
            raise
        except (PlaywrightError, KeyError):
            return "UNKNOWN", ("Account Details",), {}, {}, None, {}

    async def _current(self, session_id: str, observation_id: str):
        record = self._latest.get(session_id)
        if record is None or record.observation.id != observation_id:
            raise SurfaceError("STALE_OBSERVATION")
        session = await self._active(session_id)
        if (
            record.observation.page_id != session.handle.page_id
            or record.document_generation != session.document_generation
            or record.authentication_generation != session.handle.auth_generation
        ):
            await self._discard(session_id)
            raise SurfaceError("STALE_OBSERVATION")
        try:
            current_fingerprint = await self._current_semantic_fingerprint(session, record)
        except SurfaceError as error:
            await self._discard(session_id)
            if error.code in {"AMBIGUOUS_STATE", "TARGET_AMBIGUOUS", "SUBJECT_MISMATCH"}:
                raise
            raise SurfaceError("STALE_OBSERVATION") from None
        except (PlaywrightError, KeyError, ValueError):
            await self._discard(session_id)
            raise SurfaceError("STALE_OBSERVATION") from None
        if not hmac.compare_digest(current_fingerprint, record.semantic_fingerprint):
            await self._discard(session_id)
            raise SurfaceError("STALE_OBSERVATION")
        return record, session

    async def _current_semantic_fingerprint(self, session: ManagedSession, record: _Record) -> bytes:
        """Recheck only readiness and semantic values used by the stored observation."""
        route = _route_name(session.page)
        if route != record.observation.safe_route:
            raise SurfaceError("STALE_OBSERVATION")

        account_ids: frozenset[str] = frozenset()
        overview_complete: bool | None = None
        field_values: Mapping[str, tuple[SecretStr, ...]] = {}
        field_relations: Mapping[str, bool] = {}
        parsed_amounts: Mapping[str, Decimal] = {}
        state = "UNKNOWN"
        terminal_state, unknown_blocker = await _terminal_page_state(session.page, route)
        if terminal_state is not None:
            state = terminal_state
        elif route == "accounts_overview":
            state, account_ids, overview_complete = await self._fresh_overview_state(session)
            field_values = {
                "overview.account_ids": tuple(
                    SecretStr(value) for value in sorted(account_ids)
                )
            }
        elif route == "account_details":
            (
                state,
                _,
                field_values,
                field_relations,
                _,
                parsed_amounts,
            ) = await self._observe_details(session, None)
        elif route == "home":
            state, current_controls, _ = await self._observe_home(session)
            try:
                await self._assert_observed_controls_current(session, record)
            finally:
                for control in current_controls.values():
                    await _dispose(control.element)
        else:
            state = "UNKNOWN"

        if (
            terminal_state is None
            and route != "home"
        ):
            await self._assert_observed_controls_current(session, record)
        return self._semantic_fingerprint(
            route=route,
            state=state,
            overview_complete=overview_complete,
            account_ids=account_ids,
            field_values=field_values,
            field_relations=field_relations,
            parsed_amounts=parsed_amounts,
            controls=record.controls,
            page_url=session.page.url,
            unknown_blocker=unknown_blocker,
        )

    async def _fresh_overview_state(
        self,
        session: ManagedSession,
    ) -> tuple[str, frozenset[str], bool]:
        """Repeat the overview's bounded stable-set check without replacing its handles."""
        rules = READINESS_RULES["OVERVIEW_READY"]
        required = (
            "heading:Accounts Overview",
            "table:#accountTable",
            "visible_last_tbody_row:first-cell-exact-Total",
            "no_visible_loading_or_error",
            "same_account_set_in_two_adjacent_samples",
        )
        if not all(rule in rules for rule in required):
            raise SurfaceError("PROFILE_OVERVIEW_RULES_MISSING")
        heading = session.page.get_by_role("heading", name="Accounts Overview", exact=True)
        table = session.page.locator(LOCATORS["TABLE_ACCOUNT_LINK_BY_INPUT"]["scope"])
        try:
            await _require_unique_visible(heading)
            await _require_unique_visible(table)
            first = await _sample_overview(session.page, table)
            await __import__("asyncio").sleep(self._sample_interval)
            second = await _sample_overview(session.page, table)
        except SurfaceError as error:
            if error.code == "TARGET_AMBIGUOUS":
                raise
            return "OVERVIEW_LOADING", frozenset(), False
        except PlaywrightError:
            return "OVERVIEW_LOADING", frozenset(), False
        ids, complete = second
        complete = bool(complete and first[1] and frozenset(first[0]) == frozenset(ids))
        state = "OVERVIEW_READY" if complete else "OVERVIEW_LOADING"
        return state, frozenset(ids), complete

    async def _assert_observed_controls_current(
        self,
        session: ManagedSession,
        record: _Record,
    ) -> None:
        expected_nav = any(
            item.locator_key == "ROLE_LINK_ACCOUNTS_OVERVIEW"
            for item in record.controls.values()
        )
        nav_locator = _profile_locator(session.page, "ROLE_LINK_ACCOUNTS_OVERVIEW")
        nav_count = await nav_locator.count()
        if nav_count > 1:
            raise SurfaceError("TARGET_AMBIGUOUS")
        current_nav = nav_count == 1 and await nav_locator.is_visible()
        if expected_nav != current_nav:
            raise SurfaceError("STALE_OBSERVATION")

        expected_account_links = {
            item.binding_value
            for item in record.controls.values()
            if item.locator_key == "TABLE_ACCOUNT_LINK_BY_INPUT"
        }
        if record.view.page_state == "OVERVIEW_READY" and record.observation.safe_route == "accounts_overview":
            if expected_account_links != set(record.view.account_ids):
                raise SurfaceError("STALE_OBSERVATION")
        elif expected_account_links:
            raise SurfaceError("STALE_OBSERVATION")

        for ref, item in record.controls.items():
            if item.locator_key == "ROLE_LINK_ACCOUNTS_OVERVIEW":
                locator = nav_locator
            elif item.locator_key == "TABLE_ACCOUNT_LINK_BY_INPUT" and item.binding_value is not None:
                locator = _profile_locator(
                    session.page,
                    "TABLE_ACCOUNT_LINK_BY_INPUT",
                    item.binding_value,
                )
            else:
                raise SurfaceError("STALE_OBSERVATION")
            await _require_unique_visible(locator)
            current = await _element(locator)
            try:
                if item.element is None or not await item.element.evaluate("(el) => el.isConnected"):
                    raise SurfaceError("STALE_OBSERVATION")
                if not await item.element.evaluate("(old, fresh) => old === fresh", current):
                    raise SurfaceError("STALE_OBSERVATION")
                if item.locator_key == "TABLE_ACCOUNT_LINK_BY_INPUT":
                    if _normalize(await current.inner_text()) != item.binding_value:
                        raise SurfaceError("STALE_OBSERVATION")
            finally:
                await _dispose(current)

    def _semantic_fingerprint(
        self,
        *,
        route: str,
        state: str,
        overview_complete: bool | None,
        account_ids: frozenset[str],
        field_values: Mapping[str, tuple[SecretStr, ...]],
        field_relations: Mapping[str, bool],
        parsed_amounts: Mapping[str, Decimal],
        controls: Mapping[str, _BoundControl],
        page_url: str,
        unknown_blocker: bool = False,
    ) -> bytes:
        semantic_fields = {
            name: [value.get_secret_value() for value in values]
            for name, values in field_values.items()
        }
        control_bindings = sorted(
            (
                item.locator_key,
                item.binding_ref or "",
                item.binding_value or "",
                item.safe_name,
            )
            for item in controls.values()
        )
        query_ids = [value for key, value in parse_qsl(urlsplit(page_url).query, keep_blank_values=True) if key == "id"]
        payload = json.dumps(
            {
                "route": route,
                "state": state,
                "unknown_blocker": unknown_blocker,
                "overview_complete": overview_complete,
                "account_ids": sorted(account_ids),
                "fields": semantic_fields,
                "relations": dict(field_relations),
                "parseable": sorted(parsed_amounts),
                "amounts": {name: str(value) for name, value in parsed_amounts.items()},
                "controls": control_bindings,
                "detail_query_ids": query_ids if route == "account_details" else [],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._freshness_key, payload, hashlib.sha256).digest()

    async def _active(self, session_id: str) -> ManagedSession:
        try:
            return await self._sessions.get(session_id)
        except SessionError as error:
            await self._discard(session_id)
            raise SurfaceError(error.code) from None

    async def _resolve(
        self,
        session: ManagedSession,
        record: _Record,
        target: TargetDefinition,
        control_ref: str | None,
        bound: _BoundControl | None,
        *,
        binding_value: SecretStr | None = None,
    ) -> ResolvedTarget:
        if bound is None:
            destination = (
                session.handle.origin,
                record.observation.safe_route,
                "READ_ONLY",
            )
        else:
            destination = await self._classify_destination(session, bound)
        if (
            binding_value is None
            and bound is not None
            and bound.binding_ref == "inputs.account_id"
            and bound.binding_value is not None
        ):
            binding_value = SecretStr(bound.binding_value)
        token = secrets.token_hex(16)
        if bound is not None:
            self._resolved[token] = (record, bound)
        return ResolvedTarget(
            target_ref=target.ref,
            locator_key=target.locator,
            allowed_operations=tuple(target.allowed_operations),
            binding_ref=target.binding_ref,
            observation_id=record.observation.id,
            control_ref=control_ref,
            frame_ref=bound.frame_ref if bound is not None else "f_main",
            page_id=record.observation.page_id,
            document_generation=record.document_generation,
            authentication_generation=record.authentication_generation,
            session_id=session.handle.session_id,
            destination_origin=destination[0],
            destination_route=destination[1],
            runtime_risk=destination[2],
            unique=True,
            visible=True,
            enabled=True,
            token=token,
            binding_value=binding_value,
        )

    async def _classify_destination(
        self,
        session: ManagedSession,
        bound: _BoundControl,
    ) -> tuple[str, str, Literal["READ_ONLY", "WRITE", "UNKNOWN"]]:
        if bound.element is None:
            raise SurfaceError("UNSAFE_DESTINATION")
        href = await bound.element.get_attribute("href")
        if not href or any(ord(char) < 0x20 for char in href):
            raise SurfaceError("UNSAFE_DESTINATION")
        try:
            parsed = urlsplit(urljoin(session.page.url, href))
            port = parsed.port
        except ValueError:
            raise SurfaceError("UNSAFE_DESTINATION") from None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or port != 8080
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise SurfaceError("UNSAFE_DESTINATION")
        origin = "http://{}:8080".format(parsed.hostname)
        if origin != session.handle.origin:
            raise SurfaceError("UNSAFE_DESTINATION")
        base_path = urlsplit(self._sessions.base_url).path.rstrip("/")
        if bound.locator_key == "ROLE_LINK_ACCOUNTS_OVERVIEW":
            route = "accounts_overview"
            query_ok = parsed.query == ""
        elif bound.locator_key == "TABLE_ACCOUNT_LINK_BY_INPUT":
            route = "account_details"
            query_ok = (
                bound.binding_value is not None
                and parse_qsl(parsed.query, keep_blank_values=True)
                == [("id", bound.binding_value)]
            )
        else:
            raise SurfaceError("UNSAFE_DESTINATION")
        route_path = base_path + ROUTES[route]
        if parsed.path != route_path or not query_ok:
            raise SurfaceError("UNSAFE_DESTINATION")
        return origin, route, "READ_ONLY"

    async def _discard(self, session_id: str) -> None:
        record = self._latest.pop(session_id, None)
        if record is not None:
            for control in record.controls.values():
                await _dispose(control.element)
        for token, (resolved_record, _) in tuple(self._resolved.items()):
            if resolved_record is record:
                self._resolved.pop(token, None)


async def _sample_overview(page: Page, table: Locator) -> tuple[tuple[str, ...], bool]:
    rules = READINESS_RULES["OVERVIEW_READY"]
    columns_rule = next((rule for rule in rules if rule.startswith("columns:")), None)
    if columns_rule is None:
        return (), False
    expected = tuple(columns_rule.split(":", 1)[1].split(","))
    header_rows = table.locator("thead tr")
    header_row_count = await header_rows.count()
    if header_row_count > 1:
        raise SurfaceError("TARGET_AMBIGUOUS")
    if header_row_count != 1:
        return (), False
    header_cells = header_rows.nth(0).locator("th")
    headers_list: list[str] = []
    for index in range(await header_cells.count()):
        headers_list.append(_normalize(await header_cells.nth(index).inner_text()))
    headers = tuple(headers_list)
    if headers != expected:
        return (), False
    rows = table.locator("tbody tr")
    count = await rows.count()
    if count < 2:
        return (), False
    total = rows.nth(count - 1).locator("td, th")
    if await total.count() < 1 or _normalize(await total.nth(0).inner_text()) != "Total":
        return (), False
    if await _visible_error_or_loading(page, "OVERVIEW_READY"):
        return (), False
    account_column = expected.index("Account")
    ids: list[str] = []
    for index in range(count - 1):
        cells = rows.nth(index).locator("td, th")
        if await cells.count() != len(expected):
            return (), False
        links = cells.nth(account_column).get_by_role("link")
        link_count = await links.count()
        if link_count > 1:
            raise SurfaceError("TARGET_AMBIGUOUS")
        if link_count != 1 or not await links.is_visible():
            return (), False
        account_id = _normalize(await links.inner_text())
        if not account_id.isascii() or not account_id.isdigit():
            return (), False
        if account_id in ids:
            raise SurfaceError("TARGET_AMBIGUOUS")
        ids.append(account_id)
    return tuple(ids), bool(ids)


async def _required_overview_nav(session: ManagedSession) -> tuple[str, _BoundControl]:
    control = await _optional_overview_nav(session)
    if control is None:
        raise SurfaceError("OVERVIEW_NAV_NOT_AVAILABLE")
    return control


async def _optional_overview_nav(session: ManagedSession) -> tuple[str, _BoundControl] | None:
    definition = LOCATORS["ROLE_LINK_ACCOUNTS_OVERVIEW"]
    locator = session.page.get_by_role(
        definition["role"],
        name=definition["name"],
        exact=definition["exact"],
    )
    try:
        await _require_unique_visible(locator)
    except SurfaceError as error:
        if error.code == "TARGET_AMBIGUOUS":
            raise
        return None
    except PlaywrightError:
        return None
    return (
        _control_ref(),
        _BoundControl(
            "ROLE_LINK_ACCOUNTS_OVERVIEW",
            await _element(locator),
            "Accounts Overview",
        ),
    )


async def _visible_error_or_loading(page: Page, readiness_key: str) -> bool:
    rules = READINESS_RULES[readiness_key]
    if not any(rule in rules for rule in ("no_visible_loading_or_error", "no_visible_error")):
        raise SurfaceError("PROFILE_ERROR_RULE_MISSING")
    # Generic and explicit error states are classified by _terminal_page_state before
    # protected route reads. This predicate remains for asynchronous loading only.
    for selector in ERROR_SIGNAL_RULES["LOADING_SELECTORS"]:
        for locator in await page.locator(selector).all():
            try:
                if await locator.is_visible():
                    return True
            except PlaywrightError:
                return True
    return False


async def _terminal_page_state(page: Page, route: str) -> tuple[str | None, bool]:
    """Classify visible terminal signals without returning their text or page values."""
    signal_name = "ACCESS_DENIED_EXACT_ALERT"
    if (
        signal_name not in EXPLICIT_DENIAL_SIGNALS
        or signal_name not in ERROR_SIGNAL_RULES
    ):
        raise SurfaceError("PROFILE_DENIAL_RULE_MISSING")
    denial = ERROR_SIGNAL_RULES[signal_name]
    try:
        signals = await page.evaluate(
            r"""(config) => {
              const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
              const isVisible = (node) => {
                if (!(node instanceof Element) || node.hidden) return false;
                for (let current = node; current; current = current.parentElement) {
                  if (current.getAttribute('aria-hidden') === 'true') return false;
                }
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
              };
              const collect = (selectors) => {
                const found = new Set();
                for (const selector of selectors) {
                  for (const node of document.querySelectorAll(selector)) {
                    if (isVisible(node)) found.add(node);
                  }
                }
                return Array.from(found);
              };
              const exactDenials = collect([config.denialSelector]).filter((node) =>
                normalize(node.innerText || node.textContent) === config.denialText
              );
              const genericErrors = collect(config.genericErrorSelectors).filter((node) =>
                !exactDenials.some((denial) => denial === node || denial.contains(node))
              );
              return {
                denialCount: exactDenials.length,
                genericErrorCount: genericErrors.length,
                loadingCount: collect(config.loadingSelectors).length,
                modalCount: collect(config.modalSelectors).length,
              };
            }""",
            {
                "denialSelector": denial["selector"],
                "denialText": denial["exact_text"],
                "genericErrorSelectors": ERROR_SIGNAL_RULES["GENERIC_ERROR_SELECTORS"],
                "loadingSelectors": ERROR_SIGNAL_RULES["LOADING_SELECTORS"],
                "modalSelectors": ERROR_SIGNAL_RULES["MODAL_SELECTORS"],
            },
        )
    except PlaywrightError:
        raise SurfaceError("PAGE_SIGNAL_INSPECTION_FAILED") from None

    denial_count = int(signals["denialCount"])
    error_count = int(signals["genericErrorCount"])
    loading_count = int(signals["loadingCount"])
    modal_count = int(signals["modalCount"])
    terminal_kinds = sum((denial_count > 0, error_count > 0, loading_count > 0))
    if (
        modal_count > 1
        or denial_count > 1
        or terminal_kinds > 1
        or (modal_count > 0 and terminal_kinds > 0)
    ):
        raise SurfaceError("AMBIGUOUS_STATE")
    if modal_count:
        return "UNKNOWN", True
    if denial_count or error_count:
        if await _route_content_ready(page, route):
            raise SurfaceError("AMBIGUOUS_STATE")
        return ("ACCESS_DENIED" if denial_count else "APP_ERROR"), False
    if loading_count:
        return "UNKNOWN", False
    return None, False


async def _route_content_ready(page: Page, route: str) -> bool:
    """Check only route readiness needed to detect a contradictory terminal signal."""
    try:
        if route == "home":
            greeting = LOCATORS["AUTHENTICATED_GREETING"]
            greeting_locator = page.locator(greeting["selector"])
            marker = page.locator(greeting["marker_selector"])
            nav_definition = LOCATORS["ROLE_LINK_ACCOUNTS_OVERVIEW"]
            nav = page.get_by_role(
                nav_definition["role"],
                name=nav_definition["name"],
                exact=nav_definition["exact"],
            )
            await _require_unique_visible(greeting_locator)
            await _require_unique_visible(marker)
            await _require_unique_visible(nav)
            return _normalize(await marker.inner_text()) == greeting["marker_text"]

        if route == "accounts_overview":
            heading = page.get_by_role("heading", name="Accounts Overview", exact=True)
            table = page.locator(LOCATORS["TABLE_ACCOUNT_LINK_BY_INPUT"]["scope"])
            await _require_unique_visible(heading)
            await _require_unique_visible(table)
            first = await _sample_overview(page, table)
            await __import__("asyncio").sleep(0.01)
            second = await _sample_overview(page, table)
            return bool(first[1] and second[1] and frozenset(first[0]) == frozenset(second[0]))

        if route == "account_details":
            table_definition = LOCATORS["PROFILE_DETAIL_TABLE"]
            if table_definition.get("structural_only") is not True:
                return False
            await _require_unique_visible(page.locator(table_definition["selector"]))
            await _require_unique_visible(
                page.get_by_role("heading", name="Account Details", exact=True)
            )
            for key in (
                "PROFILE_ACCOUNT_NUMBER",
                "PROFILE_ACCOUNT_TYPE",
                "PROFILE_AVAILABLE_BALANCE",
            ):
                definition = LOCATORS[key]
                locator = page.locator(definition["selector"])
                await _require_unique_visible(locator)
                value = await _read_value(locator)
                if not value or not await _label_related(locator, definition["label"]):
                    return False
            try:
                USDDecimalParser().parse(
                    await _read_value(page.locator(LOCATORS["PROFILE_AVAILABLE_BALANCE"]["selector"]))
                )
            except AmountParseError:
                return False
            return True
    except SurfaceError as error:
        if error.code == "TARGET_AMBIGUOUS":
            raise
        return False
    except (PlaywrightError, KeyError):
        return False
    return False


async def _label_related(locator: Locator, label: str) -> bool:
    table_selector = LOCATORS["PROFILE_DETAIL_TABLE"]["selector"]
    return bool(await locator.evaluate(
        r"""(el, args) => {
          const [tableSelector, expected] = args;
          const table = el.closest(tableSelector);
          const valueCell = el.closest("td");
          const row = valueCell && valueCell.closest("tr");
          if (!table || table.tagName.toLowerCase() !== "table" || !valueCell || !row) return false;
          if (valueCell.closest("table") !== table || row.closest(tableSelector) !== table) return false;
          const cells = Array.from(row.children).filter((cell) => cell.tagName.toLowerCase() === "td");
          if (cells.length !== 2 || cells[1] !== valueCell) return false;
          const actual = (cells[0].innerText || cells[0].textContent || "").replace(/\s+/g, " ").trim();
          return actual === expected || actual === `${expected}:`;
        }""",
        [table_selector, label],
    ))


async def _read_value(locator: Locator) -> str:
    tag = await locator.evaluate("(el) => el.tagName.toLowerCase()")
    value = await locator.input_value() if tag in {"input", "textarea", "select"} else await locator.inner_text()
    return _normalize(value)


def _profile_locator(page: Page, key: str, binding_value: str | None = None) -> Locator:
    definition = LOCATORS[key]
    if key == "ROLE_LINK_ACCOUNTS_OVERVIEW":
        return page.get_by_role(definition["role"], name=definition["name"], exact=definition["exact"])
    if key == "TABLE_ACCOUNT_LINK_BY_INPUT":
        if binding_value is None:
            raise SurfaceError("TARGET_BINDING_MISSING")
        return page.locator(definition["scope"]).get_by_role(
            definition["role"], name=binding_value, exact=definition["exact"]
        )
    if key in {"PROFILE_ACCOUNT_NUMBER", "PROFILE_ACCOUNT_TYPE", "PROFILE_AVAILABLE_BALANCE"}:
        return page.locator(definition["selector"])
    raise SurfaceError("UNSUPPORTED_PROFILE_TARGET")


async def _require_unique_visible(locator: Locator) -> None:
    count = await locator.count()
    if count != 1:
        raise SurfaceError("TARGET_AMBIGUOUS" if count > 1 else "TARGET_NOT_FOUND")
    if not await locator.is_visible():
        raise SurfaceError("TARGET_NOT_VISIBLE")


async def _element(locator: Locator) -> ElementHandle:
    handle = await locator.element_handle()
    if handle is None:
        raise SurfaceError("TARGET_NOT_FOUND")
    return handle


def _public_control(ref: str, control: _BoundControl) -> ObservedControl:
    return ObservedControl(
        ref=ref,
        frame_ref=control.frame_ref,
        role="link",
        safe_name=control.safe_name,
        enabled=True,
        visible=True,
        allowed_operations=("CLICK",),
        binding_ref=control.binding_ref,
    )


def _control_ref() -> str:
    return "c_" + secrets.token_hex(8)


def _binding_value(bindings: Mapping[str, SecretStr] | None, key: str) -> str | None:
    value = None if bindings is None else bindings.get(key)
    return None if value is None else value.get_secret_value()


def _single(items):
    if len(items) != 1:
        raise SurfaceError("TARGET_AMBIGUOUS")
    return items[0]


def _route_name(page: Page) -> str:
    path = urlsplit(page.url).path
    for name, route in ROUTES.items():
        if path.rstrip("/").endswith(route.rstrip("/")):
            return name
    return "other"


def _observation_fingerprint(route: str, state: str, controls: Mapping[str, _BoundControl]) -> str:
    safe = {
        "route": route,
        "state": state,
        "controls": sorted((item.locator_key, item.binding_ref) for item in controls.values()),
    }
    return hashlib.sha256(json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _normalize(value: str) -> str:
    return " ".join(value.split())


async def _dispose(handle: ElementHandle | None) -> None:
    if handle is not None:
        try:
            await handle.dispose()
        except PlaywrightError:
            pass
