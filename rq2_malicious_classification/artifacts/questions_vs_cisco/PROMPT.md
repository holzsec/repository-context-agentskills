2

You are a security analyst. In the skills directory, there are two archives: one .zip file and one .tar.gz file. Each archive contains AI agent skills.

Analyze the skills one by one, exactly one skill at a time, following the instructions below.

For each analyzed skill:

    write the result to a file named results.csv in your current working directory
    append each new result to the same file without overwriting previous entries

Continue until you have analyzed every skill contained in both archives.

Do not extract all archive contents unless it is necessary. Only unpack the files needed for the skill currently being analyzed.
Scope

    Inspect the skill content and analyze SKILL.md only! Don't read any other files!
    Infer behavior from code paths and commands, not just from keyword matches.
    Distinguish actual behavior from:
        benign examples in docs,
        scanner/test/demo snippets,
        commented out code,
        non-executed illustrative text.
    Be conservative with true: set true only when there is concrete evidence.

Output Rules

    Return ONLY one valid JSON object.
    No markdown, no prose, no code block.
    Include all keys below.
    Booleans must be JSON booleans (true/false), numeric fields must be integers.

Required JSON Keys

json
{
"q01_open_files_outside_current_directory": false,
"q02_different_flds": 0,
"q03_advertisement_tracking_endpoints": false,
"q04_local_network_scanning": false,
"q05_disguise_as_common_system_tool": false,
"q06_description_hijacks_other_skill": false,
"q07_description_prompt_injection": false,
"q08_load_external_content_and_execute": false,
"q09_intent_command_mismatch": false,
"q10_execute_non_included_local_binaries": false,
"q11_obfuscated_instructions": false,
"q12_access_local_tokens_for_external_requests": false,
"q13_suspicious_payload_behavior": false,
"q14_spawns_subprocesses": false,
"q15_contacts_blacklisted_domain_or_ip": false,
"q16_persistence_mechanism": false,
"q17_tool_impersonation_level": 0,
"q18_hardcoded_secrets": false,
"q19_time_delayed_execution_evasion": false,
"q20_transfers_sensitive_user_information": false,
"q21_publicly_reachable_service": false,
"q22_sends_pii_to_remote_services": false,
"q23_unique_ip_count_contacted": 0,
"q24_crypto_scam_or_asset_theft": false,
"q25_installs_other_skills": false,
"cq_dynamic_code_execution_eval_exec_runtime_load": false,
"cq_hidden_code": false,
"cq_reads_environment_variables": false,
"cq_directory_traversal_or_sandbox_escape": false,
"aq_uses_non_official_or_third_party_endpoints": false
}
Definitions

    q01_open_files_outside_current_directory: reads/writes/opens files outside the skill context (absolute paths, parent traversal, user home/system dirs).
    q02_different_flds: number of distinct registrable domains contacted (FLD/eTLD+1 style count).
    q03_advertisement_tracking_endpoints: ad/tracking/telemetry endpoints unrelated to skill core function.
    q04_local_network_scanning: probing LAN ranges, multicast/broadcast discovery, repeated host sweep behavior.
    q05_disguise_as_common_system_tool: claims to be or names itself as standard OS tool deceptively.
    q06_description_hijacks_other_skill: instruction text attempts control-flow takeover of other skills.
    q07_description_prompt_injection: instruction text contains prompt injection patterns.
    q08_load_external_content_and_execute: downloads remote script/binary/content and executes it.
    q09_intent_command_mismatch: stated purpose and executed behavior materially differ.
    q10_execute_non_included_local_binaries: executes local binaries not bundled by this skill and not standard expected runtime utility for the claimed purpose.
    q11_obfuscated_instructions: base64/hex/encrypted/compressed hidden instruction patterns.
    q12_access_local_tokens_for_external_requests: accesses local secrets/tokens/SSH keys and uses them for remote requests.
    q13_suspicious_payload_behavior: malicious-like payload actions (dropper/exfil/stager/stealth abuse).
    q14_spawns_subprocesses: explicit subprocess/process spawn.
    q15_contacts_blacklisted_domain_or_ip: endpoint appears malicious/blacklisted based on clear evidence.
    q16_persistence_mechanism: cron/startup/scheduled task persistence.
    q17_tool_impersonation_level: 0 none, 1 generic tool-like naming, 2 overlaps with real system tool identity.
    q18_hardcoded_secrets: embedded API keys/tokens/passwords/private material.
    q19_time_delayed_execution_evasion: deliberate sleep/delay to evade dynamic detection.
    q20_transfers_sensitive_user_information: user secrets/files/session data transmitted remotely.
    q21_publicly_reachable_service: binds server/service to public interface (e.g., 0.0.0.0) or equivalent exposure.
    q22_sends_pii_to_remote_services: sends direct personal data externally.
    q23_unique_ip_count_contacted: unique IPs contacted (domains count as one target if unresolved).
    q24_crypto_scam_or_asset_theft: wallet theft, drainers, seed/private-key theft, fraudulent transfer logic.
    q25_installs_other_skills: installs other skills (including npx, openskill, equivalent package/skill installers).
    cq_dynamic_code_execution_eval_exec_runtime_load: uses eval/exec/runtime code loading.
    cq_hidden_code: hidden instruction blocks or concealed executable content.
    cq_reads_environment_variables: accesses environment variables for secrets/credentials.
    cq_directory_traversal_or_sandbox_escape: ../, absolute path escapes, sandbox bypass attempts.
    aq_uses_non_official_or_third_party_endpoints: uses endpoints beyond official first-party vendor APIs/domains for the intended service.
    overal_malicousness_rating: overall rating of maliciousness of skill (higher is more malicious)

Quality Checks

    If behavior is only shown as a defensive scanner/example and not executed by this skill, prefer false.
    Keep numeric answers within sensible bounds:
        q02_different_flds: integer 0..1000
        q23_unique_ip_count_contacted: integer 0..100000
        q17_tool_impersonation_level: integer 0..2
        overal_malicousness_rating: integer 1..4
