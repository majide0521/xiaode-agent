from __future__ import annotations

import hmac
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import streamlit as st

from xiaode.app_runtime import (
    artifact_path,
    build_subprocess_env,
    load_manifest,
    terminate_process,
)
from xiaode.models import APP_VERSION
from xiaode.security import URLValidationError, normalize_official_domain


PROJECT_DIR = Path(__file__).resolve().parent
RUNS_ROOT = (PROJECT_DIR / "runs").resolve()


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _pump_stdout(stream: object, output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:  # type: ignore[union-attr]
            output.put(str(line).rstrip())
    finally:
        output.put(None)


st.set_page_config(page_title="小德竞品研究 Agent", page_icon="🔎", layout="wide")

configured_password = os.getenv("XIAODE_APP_PASSWORD", "")
if configured_password:
    entered_password = st.text_input("访问密码", type="password")
    if not entered_password or not hmac.compare_digest(entered_password, configured_password):
        st.info("请输入访问密码后继续。")
        st.stop()
else:
    st.sidebar.warning("当前未配置访问密码；公开部署前请设置 XIAODE_APP_PASSWORD。")

st.title("🔎 小德竞品研究 Agent")
st.caption(
    f"v{APP_VERSION} · 可验证研究流水线：安全检索 → 原子证据 → 独立审核 → 结构化报告 → 确定性引用"
)

competitor = st.text_input("竞品名称", placeholder="例如：Perplexity")
domain = st.text_input(
    "竞品官网",
    placeholder="例如：perplexity.ai，也可粘贴完整官网 URL",
    help="系统只提取官网主机名，并在访问前阻止本机、内网及保留地址。",
)
focus = st.text_area(
    "研究重点",
    placeholder="例如：重点分析它对百度搜索和 AI 搜索的威胁，以及值得借鉴的产品、增长和商业化策略",
    height=120,
)
mode_label = st.radio(
    "研究模式",
    options=("快速", "深度"),
    horizontal=True,
    help="快速模式最多 6 次搜索、8 个来源；深度模式最多 10 次搜索、12 个来源。",
)
mode = "quick" if mode_label == "快速" else "deep"

start = st.button("🚀 开始研究", type="primary", use_container_width=True)

if start:
    try:
        normalized_domain = normalize_official_domain(domain)
    except URLValidationError as exc:
        st.error(str(exc))
        st.stop()
    if not competitor.strip():
        st.error("请先输入竞品名称。")
        st.stop()
    if len(competitor.strip()) > 100:
        st.error("竞品名称不能超过 100 个字符。")
        st.stop()
    if len(focus.strip()) > 2_000:
        st.error("研究重点不能超过 2,000 个字符。")
        st.stop()
    missing_keys = [key for key in ("SILICONFLOW_API_KEY", "BOCHA_API_KEY") if not os.getenv(key)]
    if missing_keys:
        st.error(f"服务端缺少必要密钥配置：{', '.join(missing_keys)}")
        st.stop()

    minimum_interval = _bounded_env_int("XIAODE_MIN_RUN_INTERVAL_SECONDS", 30, 0, 600)
    last_started = float(st.session_state.get("xiaode_last_run_started", 0.0))
    now = time.time()
    if minimum_interval and now - last_started < minimum_interval:
        wait_seconds = int(minimum_interval - (now - last_started)) + 1
        st.error(f"为控制调用成本，请等待 {wait_seconds} 秒后再启动下一次研究。")
        st.stop()
    st.session_state["xiaode_last_run_started"] = now

    run_id = uuid.uuid4().hex
    run_dir = (RUNS_ROOT / run_id).resolve()
    timeout_seconds = _bounded_env_int("XIAODE_RUN_TIMEOUT_SECONDS", 900, 60, 3600)
    environment = build_subprocess_env(run_id=run_id, output_root=RUNS_ROOT, mode=mode)
    command = [
        sys.executable,
        "-u",
        str(PROJECT_DIR / "agent.py"),
        "--competitor",
        competitor.strip(),
        "--domain",
        normalized_domain,
        "--focus",
        focus.strip() or "完整竞品研究",
        "--mode",
        mode,
        "--run-id",
        run_id,
        "--output-root",
        str(RUNS_ROOT),
    ]

    status = st.status("正在启动研究 Agent…", expanded=True)
    log_box = st.empty()
    logs: list[str] = []
    output_queue: queue.Queue[str | None] = queue.Queue()
    process: subprocess.Popen[str] | None = None
    timed_out = False
    started = time.monotonic()

    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            start_new_session=(os.name == "posix"),
        )
        if process.stdout is None:
            raise RuntimeError("无法读取 Agent 输出")
        pump = threading.Thread(
            target=_pump_stdout,
            args=(process.stdout, output_queue),
            daemon=True,
        )
        pump.start()
        status.update(label="Agent 正在研究，请稍候…", state="running")
        stream_finished = False

        while True:
            if time.monotonic() - started > timeout_seconds:
                timed_out = True
                terminate_process(process)
                break
            try:
                item = output_queue.get(timeout=0.25)
                if item is None:
                    stream_finished = True
                else:
                    logs.append(item)
                    if len(logs) > 1_000:
                        del logs[:-1_000]
                    log_box.code("\n".join(logs[-250:]), language=None)
            except queue.Empty:
                pass
            if process.poll() is not None and stream_finished:
                break

        if process.stdout:
            process.stdout.close()
        return_code = process.wait(timeout=5)

        if timed_out:
            status.update(label="Agent 超时，已终止本次运行", state="error")
            st.error(f"本次运行超过 {timeout_seconds} 秒，进程及其子进程已停止。")
            st.stop()
        if return_code != 0:
            status.update(label=f"Agent 运行失败（退出码 {return_code}）", state="error")
            try:
                failure = load_manifest(run_dir)
                st.error(str(failure.get("error", "Agent 未正常完成。")))
                diagnostics = failure.get("diagnostics", {})
                raw_validation_errors = (
                    diagnostics.get("validation_errors", [])
                    if isinstance(diagnostics, dict)
                    else []
                )
                validation_errors = (
                    raw_validation_errors if isinstance(raw_validation_errors, list) else []
                )
                artifacts = failure.get("artifacts", {})
                if validation_errors or artifacts:
                    with st.expander("查看本次失败的具体诊断", expanded=True):
                        if validation_errors:
                            st.markdown("**报告校验问题**")
                            for error in validation_errors:
                                st.write(f"- {error}")
                        diagnostic_downloads = (
                            ("report_validation", "下载校验详情", "application/json"),
                            ("rejected_report", "下载被拒绝的报告", "text/markdown"),
                            ("report_draft", "下载结构化报告草稿", "application/json"),
                            ("report_normalization", "下载程序调整记录", "application/json"),
                            ("evidence", "下载证据账本", "text/markdown"),
                            ("audit", "下载审计结果", "text/markdown"),
                            ("events", "下载事件日志", "text/plain"),
                        )
                        for artifact_key, label, mime in diagnostic_downloads:
                            try:
                                diagnostic_path = artifact_path(run_dir, failure, artifact_key)
                            except (FileNotFoundError, ValueError):
                                continue
                            st.download_button(
                                label,
                                data=diagnostic_path.read_text(encoding="utf-8"),
                                file_name=f"{run_id}_{diagnostic_path.name}",
                                mime=mime,
                                key=f"failure_{run_id}_{artifact_key}",
                            )
            except (FileNotFoundError, ValueError, json.JSONDecodeError):
                st.error("Agent 未正常完成，请查看上方最后几行日志。")
            st.stop()

        manifest = load_manifest(run_dir)
        if manifest.get("status") != "complete" or manifest.get("run_id") != run_id:
            raise ValueError("本次运行的 manifest 校验失败")
        status.update(label="✅ 研究完成，报告已通过硬校验", state="complete")

        counts = manifest.get("counts", {})
        quality = manifest.get("quality", {})
        usage = manifest.get("usage", {})
        metric_columns = st.columns(5)
        metric_columns[0].metric("来源", counts.get("sources", 0))
        metric_columns[1].metric("证据", counts.get("evidence", 0))
        metric_columns[2].metric("通过", counts.get("approved", 0))
        metric_columns[3].metric("谨慎使用", counts.get("caution", 0))
        metric_columns[4].metric("证据质量", quality.get("overall", "—"))
        st.caption(
            f"搜索 {counts.get('queries', 0)} 次 · "
            f"模型请求 {usage.get('requests', 0)} 次 · "
            f"总 Token {usage.get('total_tokens', 0)}"
        )

        report_tab, evidence_tab, audit_tab, sources_tab = st.tabs(
            ["📊 最终报告", "🧾 证据账本", "🛡️ 审计结果", "🔗 原始来源"]
        )
        report_path = artifact_path(run_dir, manifest, "report")
        report_text = report_path.read_text(encoding="utf-8")
        with report_tab:
            st.markdown(report_text)
            st.download_button(
                "⬇️ 下载研究报告",
                data=report_text,
                file_name=str(manifest.get("download_name", "competitor_report.md")),
                mime="text/markdown",
                use_container_width=True,
            )
        with evidence_tab:
            evidence_text = artifact_path(run_dir, manifest, "evidence").read_text(encoding="utf-8")
            st.markdown(evidence_text)
            st.download_button(
                "下载证据账本",
                data=evidence_text,
                file_name=f"{run_id}_evidence.md",
                mime="text/markdown",
            )
        with audit_tab:
            audit_text = artifact_path(run_dir, manifest, "audit").read_text(encoding="utf-8")
            st.markdown(audit_text)
            st.download_button(
                "下载审计报告",
                data=audit_text,
                file_name=f"{run_id}_audit.md",
                mime="text/markdown",
            )
        with sources_tab:
            sources_payload = json.loads(
                artifact_path(run_dir, manifest, "sources").read_text(encoding="utf-8")
            )
            source_rows = [
                {
                    "ID": item.get("source_id"),
                    "等级": item.get("source_tier"),
                    "标题": item.get("title"),
                    "域名": item.get("domain"),
                    "URL": item.get("url"),
                }
                for item in sources_payload
            ]
            st.dataframe(source_rows, use_container_width=True, hide_index=True)
        st.caption(f"运行 ID：{run_id} · 每次运行使用独立目录，不会读取其他用户的报告。")
    except Exception as exc:
        if process is not None:
            terminate_process(process)
        status.update(label="Agent 启动或结果校验失败", state="error")
        st.error(f"{type(exc).__name__}: {exc}")
