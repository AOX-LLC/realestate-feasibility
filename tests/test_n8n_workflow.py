"""n8n starts the morning run and does nothing else: the workflow is a schedule, a fixed market and
one POST with a credential kept in n8n, and its service sees none of the app's variables.

Written before the feature (5c) as attack tests: n8n is the one component here that could be
turned into a way in (an inbound webhook, a code node, a stored token, a network that reaches the
database). They were strict `xfail`s until the files existed.
"""

import json
import re
from typing import Any

import yaml
from attack_support import REPO

# Built in pieces: a literal `host:port/path` in a source file is read by the secret scanner as a
# key and its value.
API_URL = "http://" + "api" + ":4501"
WORKFLOW_PATH = REPO / "n8n" / "morning-brief.json"
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text())
ALLOWED_NODE_TYPES = {
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.set",
    "n8n-nodes-base.httpRequest",
}
SECRET_SHAPES = re.compile(
    r"Bearer\s+[A-Za-z0-9]|xoxb-|xoxp-|ntn_|secret_|sk-ant|[0-9a-f]{40,}", re.IGNORECASE
)


def workflow() -> dict[str, Any]:
    loaded = json.loads(WORKFLOW_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for key, value in node.items() for s in [key, *strings(value)]]
    if isinstance(node, list):
        return [s for value in node for s in strings(value)]
    return []


def nodes_of_type(kind: str) -> list[dict[str, Any]]:
    return [n for n in workflow()["nodes"] if n["type"] == kind]


# --- the workflow file --------------------------------------------------------------------------


def test_the_workflow_uses_only_a_schedule_a_set_and_an_http_request() -> None:
    types = [n["type"] for n in workflow()["nodes"]]

    assert set(types) == ALLOWED_NODE_TYPES
    # Two ways to start, and both are clocks: the morning run and the weekly prune.
    assert types.count("n8n-nodes-base.scheduleTrigger") == 2
    assert not [t for t in types if "webhook" in t.lower() or "code" in t.lower()]
    assert not [t for t in types if "execute" in t.lower() or "function" in t.lower()]


def test_nothing_in_the_workflow_listens_for_a_request() -> None:
    text = WORKFLOW_PATH.read_text().lower()

    assert "webhookid" not in text and "webhook" not in text
    assert "n8n-nodes-base.wait" not in text  # a wait node can resume on an incoming call


def test_every_http_node_posts_to_the_trigger_routes_of_the_api_service_only() -> None:
    nodes = nodes_of_type("n8n-nodes-base.httpRequest")

    assert nodes
    for node in nodes:
        parameters = node["parameters"]
        assert parameters["method"] == "POST"
        assert re.fullmatch(r"http://api:4501/triggers/[a-z]+", parameters["url"]), parameters
        assert parameters.get("timeout", parameters.get("options", {}).get("timeout")) == 10000
        assert node["retryOnFail"] is True and node["maxTries"] == 3
        assert node["waitBetweenTries"] == 60000
    assert COMPOSE["services"]["api"]["ports"] == [
        "127.0.0.1:4501:4501"
    ]  # and the name and port the workflow uses are the service's own


def test_the_token_is_a_credential_kept_in_n8n_never_a_value_in_the_file() -> None:
    document = workflow()

    for node in nodes_of_type("n8n-nodes-base.httpRequest"):
        assert node["parameters"]["authentication"] == "genericCredentialType"
        assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"
        assert node["credentials"] == {
            "httpHeaderAuth": {
                "id": node["credentials"]["httpHeaderAuth"]["id"],
                "name": "feasibility trigger",
            }
        }
        # No header of the node's own: one typed into the node would be a stored token.
        assert not node["parameters"].get("sendHeaders")
        assert "headerParameters" not in node["parameters"]
    found = [s for s in strings(document) if SECRET_SHAPES.search(s)]
    assert found == []


def test_the_workflow_ships_switched_off_with_no_recorded_data_in_the_chicago_time_zone() -> None:
    document = workflow()

    assert document["active"] is False
    assert "pinData" not in document and not document.get("staticData")
    assert document["settings"]["timezone"] == "America/Chicago"
    schedules = json.dumps(
        [n["parameters"] for n in nodes_of_type("n8n-nodes-base.scheduleTrigger")]
    )
    assert "0 6 * * *" in schedules


def test_the_weekly_prune_posts_a_real_prune_to_the_retention_route_on_sunday_at_three() -> None:
    nodes = {n["name"]: n for n in workflow()["nodes"]}
    schedule = nodes["Every Sunday night"]["parameters"]
    post = nodes["Start the retention prune"]["parameters"]

    assert "0 3 * * 0" in json.dumps(schedule)
    assert post["url"] == API_URL + "/triggers/retention"
    assert post["jsonBody"] == "={{ JSON.stringify({ dry_run: false }) }}"
    connections = workflow()["connections"]
    assert connections["Every Sunday night"]["main"][0][0]["node"] == "Start the retention prune"


def test_the_body_is_built_from_the_market_and_the_date_of_the_set_node_only() -> None:
    document = workflow()
    set_node = nodes_of_type("n8n-nodes-base.set")[0]
    morning = next(
        n
        for n in nodes_of_type("n8n-nodes-base.httpRequest")
        if n["parameters"]["url"].endswith("morning")
    )
    body = morning["parameters"]["jsonBody"]

    assert "dallas" in json.dumps(set_node["parameters"])
    # Only `$json.market` and `$json.as_of`: no environment, no other node, no network.
    used = set(re.findall(r"\$[A-Za-z_.]+", body))
    assert used <= {"$json.market", "$json.as_of"}
    assert "env" not in body.lower() and "require" not in body
    assert document["connections"]  # the three nodes are in one line


# --- the compose service ------------------------------------------------------------------------


def n8n() -> dict[str, Any]:
    return COMPOSE["services"]["n8n"]


def test_n8n_is_an_opt_in_profile_pinned_to_a_digest_on_localhost_only() -> None:
    service = n8n()

    assert service["profiles"] == ["schedule"]
    assert re.fullmatch(
        r"docker\.n8n\.io/n8nio/n8n:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", service["image"]
    )
    assert service["ports"] == ["127.0.0.1:4503:5678"]
    # The default stack and CI never start it.
    others = [s for name, s in COMPOSE["services"].items() if name != "n8n"]
    assert not [s for s in others if "n8n" in json.dumps(s.get("depends_on", {}))]


def test_n8n_holds_none_of_the_apps_variables_and_phones_nobody_home() -> None:
    environment = {key: str(value) for key, value in n8n()["environment"].items()}

    assert set(environment) <= {"GENERIC_TIMEZONE", "TZ", "N8N_ENCRYPTION_KEY", "NODES_EXCLUDE"} | {
        key for key in environment if key.startswith("N8N_")
    }
    for off in (
        "N8N_DIAGNOSTICS_ENABLED",
        "N8N_VERSION_NOTIFICATIONS_ENABLED",
        "N8N_TEMPLATES_ENABLED",
        "N8N_PERSONALIZATION_ENABLED",
    ):
        assert environment[off] == "false", off
    assert environment["GENERIC_TIMEZONE"] == environment["TZ"] == "America/Chicago"
    assert "env_file" not in n8n()
    for variable in ("API_READ_TOKEN", "API_TRIGGER_TOKEN", "DATABASE_URL", "NOTION_TOKEN"):
        assert variable not in json.dumps(n8n())


def test_n8n_cannot_run_a_command_read_a_file_run_code_or_listen_from_a_node() -> None:
    environment = n8n()["environment"]
    excluded = set(json.loads(environment["NODES_EXCLUDE"]))

    assert excluded >= {
        # a command, the file system, code
        "n8n-nodes-base.executeCommand",
        "n8n-nodes-base.readWriteFile",
        "n8n-nodes-base.code",
        "n8n-nodes-base.function",
        "n8n-nodes-base.functionItem",
        "n8n-nodes-base.aiTransform",
        "n8n-nodes-base.git",
        "n8n-nodes-base.ssh",
        "n8n-nodes-base.ftp",
        "@n8n/n8n-nodes-langchain.code",
        # anything that waits for a request to arrive
        "n8n-nodes-base.webhook",
        "n8n-nodes-base.respondToWebhook",
        "n8n-nodes-base.formTrigger",
        "n8n-nodes-base.wait",
        "n8n-nodes-base.localFileTrigger",
        "@n8n/n8n-nodes-langchain.chatTrigger",
        "@n8n/n8n-nodes-langchain.mcpTrigger",
    }
    assert environment["N8N_BLOCK_ENV_ACCESS_IN_NODE"] == "true"
    # An npm package installed from the editor would run its own code, whatever the list says.
    assert environment["N8N_COMMUNITY_PACKAGES_ENABLED"] == "false"


def test_n8n_shares_a_network_with_the_api_and_nothing_else() -> None:
    services = COMPOSE["services"]
    on_schedule = [name for name, s in services.items() if "schedule" in _networks(s)]

    assert sorted(on_schedule) == ["api", "n8n"]
    assert _networks(n8n()) == ["schedule"]  # not the default network, where the database is
    assert set(COMPOSE["networks"]) == {"schedule"}


def _networks(service: dict[str, Any]) -> list[str]:
    declared = service.get("networks", ["default"])
    return sorted(declared)


def test_n8n_is_hardened_like_the_other_services() -> None:
    service = n8n()

    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["pids_limit"] <= 256
    assert service["read_only"] is True
    # Read-only root, so the places n8n writes to at start are memory.
    assert any(t.startswith("/home/node/.cache:") for t in service["tmpfs"])
    assert any(t.startswith("/tmp:") for t in service["tmpfs"])  # noqa: S108
    assert service["mem_limit"]
    assert service["depends_on"]["api"]["condition"] == "service_healthy"
    volumes = service["volumes"]
    assert "./n8n:/workflows:ro" in volumes
    assert "n8ndata:/home/node/.n8n" in volumes
    assert "n8ndata" in COMPOSE["volumes"]
