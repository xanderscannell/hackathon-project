# How M-TRACE works

The quick version of the algorithms behind M-TRACE, in plain language.

## 1. Roughness

The box measures how much the truck shakes up and down over every 50 m of road, after removing slow motion like braking and body roll. That's one number per stretch, in m/s²:

$$R = \sqrt{\text{mean}\left(\text{vertical shake}^2\right)}$$

Faster driving shakes more, so every score is adjusted to what it would be at 45 mph. Then it's matched to the SEMCOG road segment the truck was on.

## 2. Potholes

A **hit** is one sharp jolt: big (over about half a g), at least 3 times rougher than the road around it, and not a one-reading glitch. Hits within 15 m of each other, in the same direction of travel, are the same spot. A spot becomes a **confirmed pothole** once it's hit on two or more passes, and its 30-day clock starts from the first hit.

## 3. Roughness to a PASER grade

On roads SEMCOG has rated, rougher roads have lower grades. One line, fitted on the September drives, captures that:

$$\text{estimated PASER} = 5.75 - 3.18 \times \ln(R)$$

Doubling the roughness takes about 2 grades off. Tested on 151 half-mile sections from the October drives, which it never saw:

| | Within one grade |
|---|---|
| **Roughness line** | **67%** |
| Always guess 6 | 56% |

Guessing 6 scores 56% only because most roads are fair (5 to 7). It can't tell a poor road from a good one. The line can: it never called a good road poor.

## 4. Why the simplest model won

Three richer models were tried on the same test: a regression on 15 vibration features (64%), IBM Granite TSPulse (62%), and TSPulse plus roughness (63%). None beat the line:

- **Roughness is most of the signal.** The extra features added little it didn't already capture.
- **There wasn't much to learn from.** With 182 training sections, models with more knobs fit noise instead of the pattern.
- **TSPulse sees shape, not size.** It rescales the vibration before reading it, but how big the jolts are is what separates poor roads from good ones.

The gaps are small (101 vs 96 vs 94 sections right), so the simple line ships: it's as good as anything else and easy to explain.

## 5. Forecasting which roads turn poor

**Gradient boosting** (hundreds of small decision trees, each fixing the last one's mistakes) learns from SEMCOG's rating history, plus road class, surface and lanes. To test it, it saw nothing after 2020 and forecast 2024:

- Of the fair and good miles it flagged as most at risk, **51% were poor by 2024**, against **21%** of all fair and good miles.
- **IBM Granite TTM** did worse (57% within one grade, against boosting's 62%), because it sees only one road's short, gappy rating history and can't learn from similar roads.

## Limits

One vehicle, 11 drives, one week, southeast Michigan. Small differences between models could change with more driving, and each new vehicle type would need its own calibration.
