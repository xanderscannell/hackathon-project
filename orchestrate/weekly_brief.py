"""Weekly brief: Granite 4 on watsonx.ai rephrases facts computed by pipeline.py.

Import: orchestrate tools import -k flow -f orchestrate/weekly_brief.py
Run:    python brief.py
"""
from pydantic import BaseModel, Field

from ibm_watsonx_orchestrate.flow_builder.flows import END, START, Flow, flow


class Facts(BaseModel):
    facts: str = Field(description="Precomputed sentences, one per line. Every number the brief may use.")


class Brief(BaseModel):
    brief: str = Field(description="The weekly brief for the road commission.")


@flow(name="road_weekly_brief", description="Write a road commission's weekly pothole brief from computed facts.",
      input_schema=Facts, output_schema=Brief)
def build(aflow: Flow) -> Flow:
    write = aflow.prompt(
        name="write_brief",
        display_name="Write the brief",
        system_prompt=[
            "You write a short weekly brief for a Michigan county road commission's maintenance supervisor.",
            "You are given facts that are already computed. Rephrase them in plain language.",
            "Use only numbers that appear in the facts, exactly as written. Do not add, round, total,",
            "or compare numbers. Never invent a road, a rating, a date, or a repair.",
            "\"As of\" a date means everything up to that date, across all drives, not on that day.",
        ],
        user_prompt=[
            "Write one paragraph of 4 to 6 sentences: what was measured, how many potholes are",
            "confirmed, the most severe ones, any getting worse, and how close the oldest is to",
            "the 30-day mark. Facts:\n{facts}",
        ],
        llm="watsonx/ibm/granite-4-h-small",
        llm_parameters={"temperature": 0, "max_new_tokens": 500},
        input_schema=Facts,
        output_schema=Brief,
    )
    aflow.sequence(START, write, END)
    return aflow
