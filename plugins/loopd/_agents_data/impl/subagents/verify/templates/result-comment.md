## 🧪 Test Plan Verification Results

| # | Item | Result | Details |
|---|------|--------|---------|
{{#each items}}
| {{index}} | {{description}} | {{#if (eq result "pass")}}✅ Pass{{else if (eq result "fail")}}❌ Fail{{else if (eq result "skip")}}⏭️ Skip{{else}}⏳ Pending{{/if}} | {{details}} |
{{/each}}

### Summary
- **Passed**: {{summary.passed}}/{{total_items}}
- **Failed**: {{summary.failed}}/{{total_items}}
- **Skipped**: {{summary.skipped}}/{{total_items}}
- **Status**: {{#if (eq status "all_pass")}}🟢 All Pass{{else if (eq status "some_fail")}}🔴 Needs Fixes{{else}}🟡 Blocked{{/if}}

{{#if failed_items}}
### Failed Item Details

{{#each failed_items}}
#### {{index}}. {{description}}
- **Expected**: {{error.expected}}
- **Actual**: {{error.actual}}
{{#if evidence}}
- **Evidence**:
  ```
  {{evidence}}
  ```
{{/if}}
{{#if suggested_fix}}
- **Suggested Fix**: {{suggested_fix}}
{{/if}}

{{/each}}
{{/if}}

{{#if skipped_items}}
### Skipped Items
{{#each skipped_items}}
- **{{index}}. {{description}}**: {{details}}
{{/each}}
{{/if}}

{{#if fixes_needed}}
### Recommended Fixes
{{#each fixes_needed}}
1. **{{description}}** (Priority: {{priority}})
{{/each}}
{{/if}}

---
🤖 Automated verification by oh-my-agents | {{timestamp}}
