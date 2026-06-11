# Rubric: dev task evaluation
# Version: 1.0 | task_type: dev

## Evaluation Dimensions

Score each dimension **1–5** using the criteria below.

### 1. requirement_adherence
Does the implementation address all explicit requirements from the original request?
- **5**: All requirements implemented correctly and completely
- **4**: All major requirements met; minor items missing or slightly off-spec
- **3**: Most requirements met but 1-2 notable gaps or misinterpretations
- **2**: Partial implementation; significant requirements unaddressed
- **1**: Fundamentally misses the requirements or implements the wrong thing

### 2. code_quality
Is the code clean, readable, idiomatic, and maintainable?
- **5**: Excellent naming, minimal complexity, idiomatic patterns, no dead code
- **4**: Mostly clean with minor style or complexity issues
- **3**: Readable but with notable issues (magic numbers, deep nesting, long functions)
- **2**: Hard to follow, significant style or structural problems
- **1**: Unintelligible, copy-paste heavy, or deeply flawed structure

### 3. correctness
Does the implementation work correctly — logic, edge cases, data handling?
- **5**: Correct implementation with proper edge-case handling
- **4**: Correct for main paths; minor edge-case gaps
- **3**: Works for happy path but has known gaps or subtle bugs
- **2**: Has significant logic errors that would cause failures in normal use
- **1**: Fundamentally broken — would not function for the intended purpose

### 4. test_coverage
Are there appropriate tests (or justified absence of tests)?
- **5**: Comprehensive tests for key paths and edge cases; smoke tests present
- **4**: Tests for main flows; some edge cases missing
- **3**: Basic tests exist but coverage is thin
- **2**: Minimal or trivial tests that don't validate the actual behavior
- **1**: No tests despite the task requiring testable new behavior

### 5. pr_quality
Is the PR/handoff description clear, accurate, and useful for reviewers?
- **5**: Clear summary of changes, rationale, and testing steps; no reviewer confusion
- **4**: Good description with minor gaps
- **3**: Adequate but missing context reviewers would need
- **2**: Sparse or inaccurate description
- **1**: No description or actively misleading

## Scoring Weights
- overall_score = (requirement_adherence × 0.30) + (correctness × 0.30) + (code_quality × 0.20) + (test_coverage × 0.10) + (pr_quality × 0.10)
- Round to 2 decimal places.

## Output Format
Return ONLY a JSON object with keys: overall_score, dimension_scores, reasoning.
dimension_scores keys: requirement_adherence, code_quality, correctness, test_coverage, pr_quality.
reasoning: concise explanation (max 300 words) covering strengths and specific weaknesses.
