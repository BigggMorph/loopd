# Rubric: query task evaluation
# Version: 1.0 | task_type: query

## Evaluation Dimensions

Score each dimension **1–5** using the criteria below.

### 1. correctness
Is the answer factually correct and logically sound?
- **5**: Fully correct; no errors in reasoning or facts
- **4**: Correct with minor inaccuracies that don't affect the core answer
- **3**: Mostly correct but with one notable error or gap in reasoning
- **2**: Significant errors that undermine the answer
- **1**: Incorrect answer or fundamentally flawed reasoning

### 2. clarity
Is the answer communicated clearly and at an appropriate level for the requester?
- **5**: Exceptionally clear; no ambiguity; appropriate terminology and structure
- **4**: Clear with minor clarity issues (jargon, awkward phrasing)
- **3**: Understandable but requires re-reading or has structural issues
- **2**: Hard to follow; important points buried or unclear
- **1**: Incomprehensible or so poorly structured it cannot be used

### 3. completeness
Does the answer fully address the question, including relevant context and caveats?
- **5**: Fully addresses all aspects of the question with appropriate caveats
- **4**: Addresses main question; minor sub-questions or caveats missing
- **3**: Core question answered but important context or caveats omitted
- **2**: Partial answer; significant aspects of the question unanswered
- **1**: Fails to address the question in any meaningful way

## Scoring Weights
- overall_score = (correctness × 0.45) + (completeness × 0.30) + (clarity × 0.25)
- Round to 2 decimal places.

## Output Format
Return ONLY a JSON object with keys: overall_score, dimension_scores, reasoning.
dimension_scores keys: correctness, clarity, completeness.
reasoning: concise explanation (max 300 words) covering strengths and specific weaknesses.
