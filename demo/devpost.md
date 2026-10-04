# Devpost submission (draft)

## Project name

M-TRACE

## Elevator pitch

M-TRACE turns any fleet vehicle into a road inspector: it maps roughness and potholes against Michigan's PASER ratings, tracks the 30-day liability clock, and answers through an IBM watsonx agent.

## Inspiration

Under Michigan law (MCL 691.1403), a road agency is presumed to know about a defect that has been readily apparent for 30 days. Federal-aid roads get a PASER rating once every two years, and local roads don't have to be rated at all: in 2017, about 6% of their lane miles had PASER data. Potholes mostly get reported by residents. Fleet vehicles already drive every road, every week. I wanted those trips to measure the road. The name follows the Michigan road world's habit of long technical names with acronyms you can say out loud (MDOT, PASER, SEMCOG): Michigan Transportation Roadway Assessment and Condition Evaluation.

## What it does

- A small box (ESP32, BNO085 motion sensor, GPS, microSD) rides in a vehicle. It measures roughness over every 50 m of road and flags severe hits.
- The box keeps its results on the SD card and uploads them over WiFi when it can. Roughness is corrected for speed and matched to SEMCOG's PASER road segments by position and direction of travel.
- A hit becomes a confirmed pothole once it's found on two or more passes. Each one is tracked through its lifecycle (suspected, confirmed, worsening, repaired) with its own 30-day clock, and the most severe confirmed potholes are listed first. Each confirmed pothole gets a printable work order.
- Measured roughness is converted to an estimated PASER grade, so roads with no rating get one.
- A forecast predicts which fair and good roads will fall to poor by 2028.
- IBM Granite 4 writes a weekly brief. A check rejects any draft that contains a number not found in the computed facts.
- Road Desk, an agent on watsonx Orchestrate, answers staff questions using only tools that call M-TRACE's read-only API on IBM Code Engine.

### Michigan impact

- **Who buys it:** Michigan's 83 county road agencies, which maintain 90,484 miles of road (75% of the state's roads), and its cities. Priced per vehicle per year.
- **Who rides:** boxes go on trucks the agency already runs, so coverage grows with every vehicle, not with staff. The parts come to about $100 per box (my estimate, at single-unit retail prices).
- **Michigan jobs:** boxes assembled in Michigan, and calibrated by Michigan's PASER raters, who are trained by Michigan Tech's Center for Technology and Training.
- **Why it pays:** rebuilding a road costs 5 to 8 times more per lane mile than preventive maintenance (Public Sector Consultants, 2023, cited by TRIP), and rough roads already cost the average Michigan driver $772 a year (TRIP, 2025). Knowing which fair roads are slipping is how a fixed budget goes further.

Sources for these figures are in `demo/sources.md` in the repo.

## How I built it

- The analysis is in Python (NumPy, pandas, scikit-learn) and writes JSON files that a Leaflet dashboard reads.
- The firmware runs the same detection math as the backend. I compiled the device's detection, replayed all 11 drives through it, and compared it with the backend: all 799 hits and all 5,758 roughness windows matched.
- On the IBM side:
  - The brief is an Orchestrate flow running Granite 4.
  - The Road Desk agent's Python tools call the Code Engine API, which scales to zero when idle.
  - I tested IBM Granite TSPulse embeddings as a roughness-to-PASER model.
  - I tested IBM Granite TTM, both zero-shot and fine-tuned, as a forecaster.

## Results

- I logged 11 drives covering 179 miles in southeast Michigan between Sep 27 and Oct 3. They found 572 spots: 158 confirmed potholes and 414 single hits not yet confirmed.
- I fitted the roughness-to-PASER line on the September drives and scored it on 151 sections from the October drives. It is within one grade 67% of the time, against 56% for always guessing a 6.
- The forecast was backtested on SEMCOG's own PASER history: it saw nothing after 2020 and was scored on the real 2024 ratings across 748 miles. Gradient boosting was within one grade 62% of the time (AUC 0.78 for spotting roads that fall to poor). Assuming nothing changes scored 54% (AUC 0.73).

## Challenges I ran into

- Roughness depends on speed, so I fitted the correction from the same road segments driven at different speeds.
- GPS is only accurate to about 4 m. A pothole hit on two passes lands in two slightly different places, and I had to merge those into one spot without merging neighbors.
- Some PASER ratings were older than recent rebuilds and would have taught the model the wrong answer, so I left those roads out of the fit.
- Keeping a language model honest about numbers took a hard check, not just a careful prompt.

## Accomplishments that I'm proud of

- Detection on the device matches the backend exactly on every drive.
- Every model is scored on data it never trained on, next to a simple baseline.
- No number in the brief or the agent's answers comes from the language model itself.

## What I learned

- A simple roughness line beat the more complex models at turning roughness into a PASER grade. Granite TSPulse (62% within one grade) and a ridge regression on 15 features (64%) both came in under the line's 67%, though TSPulse plus roughness was slightly better on average error (1.24 vs 1.26 grades).
- For forecasting, gradient boosting on rating history plus road class, surface and lanes beat Granite TTM, which only sees the rating history: 62% within one grade versus 57% (zero-shot) and 56% (fine-tuned).
- Saying which approach didn't win is part of the result.

## What's next

- Put a box in several fleet vehicles so passes add up faster.
- Collect a season of data, so M-TRACE can watch a road change over months instead of one week.
- Compare against a county's own repair records.

## Built with

python, numpy, pandas, scikit-learn, leaflet, ibm-watsonx-orchestrate, ibm-granite, granite-tspulse, granite-ttm, ibm-code-engine, esp32, platformio, arduino

## Try it out

https://github.com/xanderscannell/hackathon-project

Video: https://youtu.be/J80AxYNAM8M
