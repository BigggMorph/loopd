# Rubric: research task evaluation
# Version: 1.0 | task_type: research

## Evaluation Dimensions

Score each dimension **1–5** using the criteria below.

### 1. accuracy
Is the research factually correct and relevant to the stated objective?
- **5**: All claims substantiated; no factual errors; directly relevant to the research question
- **4**: Mostly accurate with minor inaccuracies or minor relevance gaps
- **3**: Generally accurate but contains some unsubstantiated claims or off-topic content
- **2**: Notable factual errors or significant irrelevance to the objective
- **1**: Mostly incorrect, fabricated, or completely off-topic

### 2. depth
Is the research sufficiently deep and comprehensive for the scope of the request?
- **5**: Exhaustive coverage of the topic; surface and nuanced aspects addressed
- **4**: Good depth; covers most important aspects with minor gaps
- **3**: Adequate depth for a basic overview but lacks nuance on key points
- **2**: Superficial treatment; important sub-topics missing
- **1**: Extremely shallow; reads like a bullet-point summary with no substance

### 3. actionability
Can a reader make concrete decisions or take concrete actions based on the findings?
- **5**: Clear, specific recommendations with tradeoffs; immediately actionable
- **4**: Mostly actionable; minor gaps in specificity or recommendation clarity
- **3**: Some actionable insights but too abstract or vague in key areas
- **2**: Findings described without clear implications or next steps
- **1**: Purely descriptive with no connection to decision-making

### 4. source_quality
Are sources credible, well-cited, and appropriate for the research question?
- **5**: Primary or authoritative sources cited; no unsupported speculation
- **4**: Good sources with minor gaps; some claims need better attribution
- **3**: Mix of good and weak sources; some important claims unsourced
- **2**: Mostly uncited or relies on low-quality sources
- **1**: No sources; pure speculation presented as fact

## Scoring Weights
- overall_score = (accuracy × 0.35) + (depth × 0.25) + (actionability × 0.25) + (source_quality × 0.15)
- Round to 2 decimal places.

## Output Format
Return ONLY a JSON object with keys: overall_score, dimension_scores, reasoning.
dimension_scores keys: accuracy, depth, actionability, source_quality.
reasoning: concise explanation (max 300 words) covering strengths and specific weaknesses.
