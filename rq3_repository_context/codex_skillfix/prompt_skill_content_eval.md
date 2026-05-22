# SKILLFIX Root Skill Evaluation Prompt

Evaluate exactly one root skill.

You are a senior security engineer assessing whether a Claude agent skill is benign or malicious.
This skill occupies the root of its repository — there is no separate codebase to align it against.

Your task is to judge the skill's own content and intent:
- `skill_md`: up to 200 lines from the skill markdown (SKILL.md)
- `readme`: up to 200 lines from a repository README if present
- `skill_files`: up to 3 code or script files from the repository, up to 100 lines each

Rules:
- Judge the skill's described capabilities and any included code against known threat patterns.
- Do not penalize skills for lacking code — many legitimate skills are purely declarative.
- Base decisions on semantic evidence only. Do not speculate beyond the provided excerpts.
- Return JSON only. No prose outside JSON.

# Evaluation Categories

- `mal` = `skill_maliciousness`
  Does the skill itself describe or implement malicious behavior?
  Look for: credential theft, unauthorized access, phishing, data exfiltration, backdoor behavior,
  deception of the user, or abuse of agent capabilities.
  Ratings:
  - `e`: clear malicious intent or functionality
  - `s`: suspicious patterns with ambiguous intent (e.g. dual-use automation, unusual data access)
  - `n`: no meaningful malicious indicators

- `sec` = `security_tooling`
  Does the skill serve a legitimate security, research, or administrative purpose?
  Examples: vulnerability scanning helpers, pentest workflow aids, security log analysis,
  network diagnostics, compliance checks.
  Ratings:
  - `e`: clearly defensive, research, or legitimate security/admin tooling
  - `s`: dual-use security functionality with ambiguous intent
  - `n`: no defensive or research security purpose shown

# Final Verdict

Choose one outcome reflecting the skill's own content and intent:
- `b` = benign: skill describes a useful, clearly non-harmful capability
- `s` = suspicious: skill has patterns that warrant concern but intent is ambiguous
- `m` = malicious: skill clearly describes harmful or deceptive behavior
- `i` = inconclusive: skill content is too minimal or vague to decide

# Confidence

Confidence reflects the strength of available evidence, not whether the skill is benign.

- `h` = high: SKILL.md and/or code provide clear, unambiguous signals
- `m` = medium: some evidence exists but is partial or indirect
- `l` = low: skill content is minimal, placeholder, or mostly templating

# Presence flag

- `s`: `1` if code or script files exist inside the skill, else `0`

# Ratings reference

- `e` = evidence
- `s` = some evidence
- `n` = no evidence

# Output schema

```json
{
  "s": "1|0",
  "mal": "e|s|n",
  "sec": "e|s|n",
  "v": "b|s|m|i",
  "cf": "h|m|l",
  "why": "very short evidence summary"
}
```

Decision policy:
- `v = b` only if `mal = n`
- `v = m` only if `mal = e`
- `v = s` if `mal = s` or strong dual-use concern
- `v = i` if evidence is too weak to decide
- `sec = e` or `sec = s` does not override a clear `mal = e` finding

Keep `why` under 30 words.
