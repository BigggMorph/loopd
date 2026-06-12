/**
 * QA Agent custom reporter — 실행 결과를 "머지 판단 증거" 리포트로 자동 종합한다.
 * RUN_QA_AGENT=1 일 때만 동작. 산출: qa/<QA_RUN_ID|latest>/report.auto.md
 * (Playwright HTML 리포트는 raw 증거 레이어, 이 파일이 의사결정 레이어 — 설계 §11.1)
 */
import type { FullResult, Reporter, TestCase, TestResult } from "@playwright/test/reporter";
import fs from "node:fs";
import path from "node:path";

type Row = {
  title: string;
  verdict: string;
  risk: string;
  source: string;
  note: string;
  durationMs: number;
};

class QAReporter implements Reporter {
  private rows: Row[] = [];

  onTestEnd(test: TestCase, result: TestResult) {
    if (process.env.RUN_QA_AGENT !== "1") return;
    const anns = [...test.annotations, ...(result as { annotations?: typeof test.annotations }).annotations ?? []];
    const ann = (t: string) => anns.find((a) => a.type === t)?.description ?? "";

    let verdict: string;
    let note = "";
    const declared = ann("verdict");
    if (declared.startsWith("SPEC-UNCLEAR")) {
      verdict = "❓ SPEC-UNCLEAR";
      note = ann("question") || declared;
    } else if (declared.startsWith("BLOCKED")) {
      verdict = "🚧 BLOCKED";
      note = declared;
    } else if (result.status === "passed") {
      verdict = "✅ PASS";
    } else if (result.status === "skipped") {
      const skipReason = anns.find((a) => a.type === "skip" || a.type === "fixme")?.description ?? "";
      verdict = "🚧 BLOCKED";
      note = skipReason || "skip 사유 미기재";
    } else {
      verdict = "❌ FAIL";
      note = "BUG/FLAKE 분류 필요 — HTML 리포트의 영상·trace 확인";
    }
    this.rows.push({
      title: test.title,
      verdict,
      risk: ann("risk") || "—",
      source: ann("source") || (test.title.startsWith("Scenario") ? "library" : "—"),
      note,
      durationMs: result.duration,
    });
  }

  onEnd(_result: FullResult) {
    if (process.env.RUN_QA_AGENT !== "1" || this.rows.length === 0) return;
    const count = (v: string) => this.rows.filter((r) => r.verdict.includes(v)).length;
    const needsHuman = this.rows.filter((r) => r.verdict.includes("FAIL") || r.verdict.includes("SPEC-UNCLEAR"));

    const lines = [
      `# 🔍 QA Agent Report (auto)`,
      ``,
      `**판정 요약: ✅ ${count("PASS")} · ❌ ${count("FAIL")} · 🚧 ${count("BLOCKED")} · ❓ ${count("SPEC-UNCLEAR")}**`,
      needsHuman.length
        ? `**머지 전 확인 필요: ${needsHuman.map((r) => r.title.split(":")[0]).join(", ")}**`
        : `머지 전 확인 필요 항목 없음`,
      ``,
      `| 시나리오 | source | risk | verdict | 비고 |`,
      `|---|---|---|---|---|`,
      ...this.rows.map(
        (r) => `| ${r.title} | ${r.source} | ${r.risk} | ${r.verdict} | ${r.note.replaceAll("|", "\\|")} |`
      ),
      ``,
      `증거(raw): Playwright HTML 리포트 — \`npx playwright show-report\` (영상·스텝 타임라인·trace)`,
      ``,
      `---`,
      `*판정은 머지 결정이 아니다. 이 리포트는 사람의 머지 판단을 위한 증거다. — qa-check*`,
      ``,
    ];

    const dir = path.join("qa", process.env.QA_RUN_ID ?? "latest");
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, "report.auto.md");
    fs.writeFileSync(file, lines.join("\n"));
    console.log(`\n[qa-reporter] ${file} 생성 — ✅ ${count("PASS")} ❌ ${count("FAIL")} 🚧 ${count("BLOCKED")} ❓ ${count("SPEC-UNCLEAR")}`);
  }
}

export default QAReporter;
