# DS 550 Course Readings (Summaries)

Weekly reading summaries for DS 550: Data Ethics and Governance. These are the
program's own summaries, used to test retrieval over course content.

## Reading 1: Algorithmic bias in admissions and hiring models

Predictive models trained on historical decisions reproduce the patterns in that
history, including discrimination. The reading walks through a case where a
resume-screening model down-weighted applicants from women's colleges because past
hires skewed male. Key points: "removing" a protected attribute does not remove
bias when proxies remain; fairness must be defined before modeling (e.g. equal
false-negative rates across groups); and a model's impact depends on where it sits
in the decision pipeline.

## Reading 2: Differential privacy, in plain terms

Differential privacy is a formal guarantee that the output of an analysis is
almost unchanged whether or not any one person's data is included, controlled by a
privacy-loss parameter epsilon. Smaller epsilon means more noise and more privacy.
The reading covers the randomized-response intuition, where noise is added (input,
output, or in the algorithm), and the trade-off between privacy budget and
accuracy. It notes that differential privacy protects individuals, not group-level
facts.

## Reading 3: The "right to explanation" and model documentation

Some regulations give people affected by an automated decision a right to a
meaningful explanation of the logic involved. The reading contrasts global
explanations (how the model works overall) with local ones (why this person got
this result), reviews methods like feature importance and counterfactual
explanations, and argues that documentation artifacts such as model cards and
datasheets for datasets should be produced during development, not after.
