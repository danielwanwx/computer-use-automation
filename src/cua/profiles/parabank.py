"""Reviewed native ParaBank route, locator, and readiness definitions.

The browser adapter consumes these definitions. Their source digest is pinned in every
capability bundle and in the validation qualification so a locator/readiness edit forces
revalidation.
"""

from types import MappingProxyType

PROFILE_ID = "parabank-native-v1"

ROUTES = MappingProxyType(
    {
        "home": "/index.htm",
        "accounts_overview": "/overview.htm",
        "account_details": "/activity.htm",
    }
)

LOCATORS = MappingProxyType(
    {
        "ROLE_LINK_ACCOUNTS_OVERVIEW": MappingProxyType(
            {
                "role": "link",
                "name": "Accounts Overview",
                "exact": True,
                "unique": True,
            }
        ),
        "TABLE_ACCOUNT_LINK_BY_INPUT": MappingProxyType(
            {
                "scope": "#accountTable",
                "column": "Account",
                "role": "link",
                "binding": "inputs.account_id",
                "exact": True,
                "unique": True,
            }
        ),
        "PROFILE_ACCOUNT_NUMBER": MappingProxyType(
            {"selector": "#accountId", "label": "Account Number", "unique": True}
        ),
        "PROFILE_ACCOUNT_TYPE": MappingProxyType(
            {"selector": "#accountType", "label": "Account Type", "unique": True}
        ),
        "PROFILE_AVAILABLE_BALANCE": MappingProxyType(
            {
                "selector": "#availableBalance",
                "label": "Available",
                "unique": True,
            }
        ),
        "PROFILE_DETAIL_TABLE": MappingProxyType(
            {
                "selector": "#accountDetails > table",
                "unique": True,
                "structural_only": True,
            }
        ),
        # These controls are consumed only by SessionManager's privileged login setup,
        # never added to a read-only discovery action space.
        "LOGIN_USERNAME": MappingProxyType(
            {
                "selector": 'form[name="login"] input[name="username"]',
                "type": "text",
                "unique": True,
                "setup_only": True,
            }
        ),
        "LOGIN_PASSWORD": MappingProxyType(
            {
                "selector": 'form[name="login"] input[name="password"]',
                "type": "password",
                "unique": True,
                "setup_only": True,
            }
        ),
        "LOGIN_SUBMIT": MappingProxyType(
            {
                "selector": 'form[name="login"] input[type="submit"][value="Log In"]',
                "role": "button",
                "name": "Log In",
                "unique": True,
                "setup_only": True,
            }
        ),
        "AUTHENTICATED_GREETING": MappingProxyType(
            {
                "selector": "#leftPanel p.smallText",
                "marker_selector": "#leftPanel p.smallText b",
                "marker_text": "Welcome",
                "customer_name_text": "remaining_text_after_marker",
                "unique": True,
                "read_name_for_ephemeral_identity_crosscheck": True,
                "never_persist_or_send_name_to_model": True,
            }
        ),
    }
)

READINESS_RULES = MappingProxyType(
    {
        "LOGIN_READY": (
            "unique_visible:LOGIN_USERNAME",
            "unique_visible:LOGIN_PASSWORD",
            "unique_visible:LOGIN_SUBMIT",
            "visible:heading:Customer Login",
        ),
        "AUTHENTICATED_HOME": (
            "unique_visible:AUTHENTICATED_GREETING",
            "static_greeting_text_exact:Welcome",
            "nonempty_customer_name_text_for_in_memory_identity_crosscheck",
            "visible_nav_link:Accounts Overview",
            "login_form_not_visible",
        ),
        "OVERVIEW_READY": (
            "heading:Accounts Overview",
            "table:#accountTable",
            "columns:Account,Balance*,Available Amount",
            "visible_last_tbody_row:first-cell-exact-Total",
            "no_visible_loading_or_error",
            "same_account_set_in_two_adjacent_samples",
        ),
        "DETAIL_READY": (
            "heading:Account Details",
            "table:#accountDetails > table",
            "unique_visible_field:Account Number",
            "unique_visible_field:Account Type",
            "unique_visible_field:Available",
            "available_balance:USD_DECIMAL_V1@1_parseable",
            "no_visible_error",
        ),
    }
)

# Only an exact, visible accessible alert carries the reviewed denial meaning. The pinned
# ParaBank revision does not emit this message in its normal flow; the focused integration
# test injects it on the page to exercise the explicit-denial branch. Other alerts and
# `.error` elements are generic application errors, never evidence of denied membership.
ERROR_SIGNAL_RULES = MappingProxyType(
    {
        "ACCESS_DENIED_EXACT_ALERT": MappingProxyType(
            {"selector": '[role="alert"]', "exact_text": "Access Denied"}
        ),
        "GENERIC_ERROR_SELECTORS": ("[role=\"alert\"]", ".error"),
        "LOADING_SELECTORS": (".loading", "[aria-busy=\"true\"]"),
        "MODAL_SELECTORS": (
            "dialog[open]",
            "[role=\"dialog\"]",
            "[role=\"alertdialog\"]",
        ),
    }
)

PAGE_STATES = (
    "LOGIN_READY",
    "AUTHENTICATED_HOME",
    "OVERVIEW_LOADING",
    "OVERVIEW_READY",
    "DETAIL_READY",
    "LOGIN",
    "APP_ERROR",
    "ACCESS_DENIED",
    "UNKNOWN",
)

EXPLICIT_DENIAL_SIGNALS: tuple[str, ...] = ("ACCESS_DENIED_EXACT_ALERT",)
