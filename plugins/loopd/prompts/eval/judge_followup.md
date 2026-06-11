# Rubric: followup task evaluation
# Version: 1.0 | task_type: followup

## Evaluation Dimensions

Score each dimension **1–5** using the criteria below.

### 1. context_awareness
Does the response properly incorporate and build on the prior context, thread history, or original task?
- **5**: Seamlessly integrates prior context; demonstrates full understanding of history
- **4**: Good context integration with minor gaps
- **3**: Shows awareness of context but misses important background details
- **2**: Superficial acknowledgment of prior work; largely treats this as a fresh task
- **1**: Ignores prior context entirely or contradicts established facts

### 2. requirement_adherence
Does the response address the specific follow-up request accurately and completely?
- **5**: Fully addresses the follow-up request; nothing missing
- **4**: Addresses main follow-up; minor items incomplete
- **3**: Partially addresses the request; at least one notable gap
- **2**: Significant portions of the follow-up request unaddressed
- **1**: Fails to address the follow-up or addresses the wrong thing

### 3. coherence
Is the response coherent and consistent with the prior work/decisions made earlier?
- **5**: Perfectly coherent; no contradictions; builds naturally on prior work
- **4**: Mostly coherent with minor inconsistencies
- **3**: Generally coherent but with one notable inconsistency or contradiction
- **2**: Multiple inconsistencies that create confusion
- **1**: Fundamentally incoherent or contradicts prior established decisions

### 4. quality
Is the output itself high quality — correctness, clarity, and completeness of the new work delivered?
- **5**: High quality output; meets the standard expected for the task type
- **4**: Good quality with minor flaws
- **3**: Acceptable quality with notable gaps or issues
- **2**: Poor quality that would require significant rework
- **1**: Unusable or incorrect output

## Scoring Weights
- overall_score = (requirement_adherence × 0.30) + (context_awareness × 0.25) + (quality × 0.25) + (coherence × 0.20)
- Round to 2 decimal places.

## Output Format
Return ONLY a JSON object with keys: overall_score, dimension_scores, reasoning.
dimension_scores keys: context_awareness, requirement_adherence, coherence, quality.
reasoning: concise explanation (max 300 words) covering strengths and specific weaknesses.
