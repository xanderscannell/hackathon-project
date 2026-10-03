"""Road Desk tools: read-only calls to the road backend through its public URL.

The URL lives in the road_api key-value connection, so a new tunnel needs one
set-credentials, not a re-import. Every miss or failure comes back as a note
the agent has to repeat, never an empty list it could fill in.

Import: orchestrate tools import -k python -f orchestrate/road_tools.py -a road_api
"""
import json
from urllib.parse import urlencode
from urllib.request import urlopen

from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.run import connections

CREDS = [{"app_id": "road_api", "type": ConnectionType.KEY_VALUE}]

# Michigan's grouping and the treatment each group calls for, one row per rating.
PASER = {
    10: ("good", "Excellent: new or newly resurfaced, no visible defects.", "Routine maintenance."),
    9: ("good", "Excellent: like new, almost no defects.", "Routine maintenance."),
    8: ("good", "Very good: little wear, a few widely spaced cracks.", "Routine maintenance; seal cracks."),
    7: ("fair", "Good: first signs of aging, more cracking.", "Preventive maintenance: crack sealing."),
    6: ("fair", "Good to fair: weathered surface, wider cracks.", "Preventive maintenance: seal coat or thin overlay."),
    5: ("fair", "Fair: obvious surface aging, block cracking, some patching.", "Preventive maintenance: overlay or surface treatment."),
    4: ("poor", "Fair to poor: distress past the surface, cracking in the wheel paths, first rutting.", "Structural improvement: strengthening overlay."),
    3: ("poor", "Poor: frequent cracking, patching and potholes.", "Structural improvement: patch and repair, then overlay."),
    2: ("poor", "Very poor: severe deterioration.", "Reconstruction."),
    1: ("poor", "Failed: total loss of pavement.", "Reconstruction."),
}


def _get(path: str, **params) -> dict:
    try:
        base = connections.key_value("road_api")["url"].rstrip("/")
        with urlopen(f"{base}{path}?{urlencode(params)}", timeout=15) as r:
            return json.load(r)
    except Exception as e:
        return {"note": f"The road backend is unreachable ({type(e).__name__}). Say so; do not guess any numbers."}


@tool(expected_credentials=CREDS)
def find_potholes(road: str = "", state: str = "", limit: int = 10) -> dict:
    """List confirmed potholes, most severe first, with each one's 30-day clock.

    Args:
        road (str): Optional road name or part of it, e.g. "Michigan Ave". Empty for all roads.
        state (str): Optional lifecycle state: Confirmed, Worsening, Repaired, PatchFailed, or Suspected (hit once, unconfirmed).
        limit (int): How many to return, at most 25.

    Returns:
        dict: "potholes" with id, road, state, severity, passes, first_seen, days_open and days_left before the
        30-day mark; "total_matching"; "note" explains a miss and must be repeated.
    """
    return _get("/api/potholes", road=road, state=state, limit=limit)


@tool(expected_credentials=CREDS)
def potholes_near_deadline(road: str = "", limit: int = 10) -> dict:
    """List confirmed potholes closest to the 30-day mark first (fewest days left), for deadline questions.

    Args:
        road (str): Optional road name or part of it. Empty for all roads.
        limit (int): How many to return, at most 25.

    Returns:
        dict: "potholes" oldest first with days_open and days_left; "note" explains a miss and must be repeated.
    """
    return _get("/api/potholes", road=road, order="oldest", limit=limit)


@tool(expected_credentials=CREDS)
def road_condition(road: str) -> dict:
    """Condition of one road the vehicle measured: rated PASER, estimated PASER, confirmed potholes.

    Args:
        road (str): Road name or part of it, e.g. "Vining" or "Telegraph Rd".

    Returns:
        dict: "matches", one per road, with the rated PASER (SEMCOG), the roughness-based estimate and how far to
        trust it, confirmed pothole count and the worst one; "note" explains a miss and must be repeated.
    """
    return _get("/api/road", name=road)


@tool(expected_credentials=CREDS)
def pothole_work_order(pothole_id: int) -> dict:
    """Work order for one pothole: location, lane, severity, pass history, 30-day clock.

    Args:
        pothole_id (int): The pothole's id from find_potholes or road_condition.

    Returns:
        dict: The work order fields; "note" explains a bad id and must be repeated.
    """
    return _get("/api/work_order", id=pothole_id)


@tool(expected_credentials=CREDS)
def network_summary() -> dict:
    """This week's computed facts: miles measured, unrated miles, confirmed potholes, the most severe, the oldest.

    Returns:
        dict: "facts", plain sentences with every number already computed.
    """
    return _get("/api/summary")


@tool()
def paser_meaning(rating: int) -> dict:
    """What a PASER rating means and the treatment it calls for (asphalt), with Michigan's good/fair/poor group.

    Args:
        rating (int): PASER rating from 1 to 10.

    Returns:
        dict: "group" (Michigan: 8-10 good, 5-7 fair, 1-4 poor), "condition", "treatment"; "note" for a bad rating.
    """
    if rating not in PASER:
        return {"note": f"{rating} is not a PASER rating; ratings run from 1 to 10."}
    group, condition, treatment = PASER[rating]
    return {"rating": rating, "group": group, "condition": condition, "treatment": treatment,
            "source": "Summary of the PASER asphalt scale; the PASER manual is the authority."}
