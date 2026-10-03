"""Write data/out/webchat.json, the IDs the dashboard needs to embed the agent's web chat.

    .venv/Scripts/orchestrate agents deploy -n road_desk   # the widget only loads a released agent
    .venv/Scripts/python orchestrate/webchat_config.py

Built here instead of `orchestrate channels webchat embed`, which fails when the login
token carries more than one audience. The file is gitignored: it holds account IDs.
"""
import json
import sys
from pathlib import Path

import jwt
from ibm_watsonx_orchestrate_clients.agents.agent_client import AgentClient
from ibm_watsonx_orchestrate_clients.common.utils import instantiate_client
from ibm_watsonx_orchestrate_core.utils.config import (
    AUTH_CONFIG_FILE, AUTH_CONFIG_FILE_FOLDER, AUTH_MCSP_TOKEN_OPT, AUTH_SECTION_HEADER,
    CONTEXT_ACTIVE_ENV_OPT, CONTEXT_SECTION_HEADER, ENV_WXO_URL_OPT, ENVIRONMENTS_SECTION_HEADER, Config)

agent_name = sys.argv[1] if len(sys.argv) > 1 else "road_desk"
cfg = Config()
env = cfg.read(CONTEXT_SECTION_HEADER, CONTEXT_ACTIVE_ENV_OPT)
url = cfg.get(ENVIRONMENTS_SECTION_HEADER, env, ENV_WXO_URL_OPT)
token = Config(AUTH_CONFIG_FILE_FOLDER, AUTH_CONFIG_FILE).get(AUTH_SECTION_HEADER)[env][AUTH_MCSP_TOKEN_OPT]

claims = jwt.decode(token, options={"verify_signature": False})
host = url.replace("https://api.", "https://", 1).split(".com")[0] + ".com"
extra = {}
if "cloud.ibm.com" in url:  # IBM Cloud instance: account from the IAM token, instance from the URL
    account, instance = claims["account"]["bss"], url.rstrip("/").split("/instances/")[1]
    region = host.split("//")[1].split(".")[0]
    extra = {"crn": f"crn:v1:bluemix:public:watsonx-orchestrate:{region}:a/{account}:{instance}::",
             "deploymentPlatform": "ibmcloud"}
else:  # trial: both ids sit in the token's CRN audience
    aud = claims["aud"]
    crn = next(a for a in (aud if isinstance(aud, list) else [aud]) if a.startswith("crn:"))
    account, instance = crn.split("sub/")[1].split(":")[:2]
agent = instantiate_client(AgentClient).get_draft_by_name(agent_name)[0]
config = {
    "orchestrationID": f"{account}_{instance}",
    "hostURL": host,
    "agentId": agent["id"],
    "agentEnvironmentId": next(e["id"] for e in agent["environments"] if e["name"] == "live"),
    **extra,
}
Path("data/out/webchat.json").write_text(json.dumps(config, indent=1))
print("wrote data/out/webchat.json for", agent_name)
