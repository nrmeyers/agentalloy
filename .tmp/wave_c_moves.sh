#!/usr/bin/env bash
# Wave C: tests/install/ -> command-group subpackages.
# Basenames preserved so root-conftest nodeid-substring markers keep matching.
set -e
cd /home/nmeyers/dev/agentalloy

for d in wiring setup service container upgrade doctor packs cli; do
  mkdir -p tests/install/$d
  [ -f tests/install/$d/__init__.py ] || touch tests/install/$d/__init__.py
done

mv() { [ -f "tests/install/$1.py" ] && git mv "tests/install/$1.py" "tests/install/$2/$1.py" || echo "SKIP(missing): $1"; }

# wiring/ — harness + code-index wiring (17)
for f in test_wire_harness test_unwire_harness test_add_command \
         test_aider_proxy_wiring test_claude_code_proxy_wiring test_continue_proxy_wiring \
         test_hermes_agent_proxy_wiring test_opencode_proxy_wiring test_qwen_code_wiring \
         test_wiring_route_contract test_sidecar_watcher test_flow_posture_invariant \
         test_code_index_wiring test_unwire_code_index \
         test_auto_wire_worktree test_git_hooks test_intake_wire_and_status; do mv $f wiring; done

# setup/ — wizard, detection, customization (9)
for f in test_simple_setup test_setup_modules test_setup_sidecar test_customize test_revalidate \
         test_detect test_recommend_host_targets test_recommend_models test_preflight; do mv $f setup; done

# service/ — server lifecycle, service management, ports, backup (7)
for f in test_server_proc test_server_lifecycle_container test_enable_service test_service_required \
         test_port_guard test_port_guard_session test_backup_restore; do mv $f service; done

# container/ — container runtime + entrypoint readiness (3)
for f in test_container_runtime test_container_runtime_readiness test_install_packs_container; do mv $f container; done

# upgrade/ — upgrade/update, install-state schema, release check (7)
for f in test_upgrade test_update test_upgrade_notice test_upgrade_state_import \
         test_release_check test_state test_state_migration; do mv $f upgrade; done

# doctor/ — doctor, verify, CLI surface shape (11)
for f in test_doctor test_doctor_code_index test_doctor_module_drift test_doctor_worktree_wiring \
         test_verify test_verify_bootstrap test_statusline \
         test_cli_output_shape test_cli_verbs test_cli_surface_07b test_dispatcher; do mv $f doctor; done

# packs/ — pack install/validate/gates/lessons (17)
for f in test_install_pack test_install_packs test_install_local_pack test_new_skill_pack \
         test_validate_pack test_validate_pack_review test_bundled_pack_manifests \
         test_bundled_skill_validation test_pack_tag_vocabulary test_pack_tier_registry_consistency \
         test_seed_corpus test_lesson_pack test_lessons_promote \
         test_ingest_gates test_review_gate test_review_gate_wiring test_review_producer_fidelity; do mv $f packs; done

# cli/ — remaining per-subcommand coverage (27)
for f in test_phase_cli test_flow_cli test_approve_cli test_reset_step test_install_reset \
         test_contract_cli test_code_cli test_knowledge_cli test_knowledge_related \
         test_stream_command test_config test_worktree_command test_cleanup test_host_sanitize \
         test_pull_models test_pull_web test_rerank_warmup test_start_rerank_server \
         test_mcp_server test_wrap test_uninstall test_runtime_artifacts \
         test_env_forwarding test_write_env test_meta_skill_delivery \
         test_adversarial test_invariants; do mv $f cli; done

# fix cross-test imports of the moved test_install_local_pack helpers
sed -i 's|from tests\.install\.test_install_local_pack import|from tests.install.packs.test_install_local_pack import|' \
  tests/install/packs/test_review_producer_fidelity.py \
  tests/install/packs/test_review_gate_wiring.py \
  tests/install/packs/test_validate_pack_review.py

echo '=== install/ root leftovers ==='
ls tests/install/*.py
echo '=== subgroup counts ==='
for d in wiring setup service container upgrade doctor packs cli; do
  printf '%-12s %3d files\n' $d $(ls tests/install/$d/test_*.py | wc -l)
done
