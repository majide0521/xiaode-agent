import streamlit as st
import subprocess
import sys
import os
import time
from pathlib import Path

st.set_page_config(
    page_title="小德竞品研究 Agent",
    page_icon="🔎",
    layout="wide"
)

st.title("🔎 小德竞品研究 Agent")

st.caption(
    "输入竞品名称、官网和研究重点，Agent 将自动完成检索、"
    "证据提取、审核和竞品研究报告生成。"
)

st.divider()

competitor = st.text_input(
    "竞品名称",
    placeholder="例如：Perplexity"
)

domain = st.text_input(
    "竞品官网",
    placeholder="例如：perplexity.ai"
)

focus = st.text_area(
    "研究重点",
    placeholder="例如：重点分析它对百度搜索和AI搜索的威胁，以及百度值得借鉴的产品、增长和商业化策略",
    height=120
)

st.divider()

start = st.button(
    "🚀 开始研究",
    type="primary",
    use_container_width=True
)

if start:

    if not competitor.strip():
        st.error("请先输入竞品名称。")
        st.stop()

    if not domain.strip():
        st.error("请先输入竞品官网。")
        st.stop()

    status = st.status(
        "正在启动研究 Agent...",
        expanded=True
    )

    log_box = st.empty()

    logs = []

    start_time = time.time()

    env = os.environ.copy()

    # 关键：彻底关闭 Python stdout 缓冲
    env["PYTHONUNBUFFERED"] = "1"

    try:

        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "agent.py"
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )

        # 把网页上的三个输入传给 agent.py 的三个 input()
        input_data = (
            competitor.strip()
            + "\n"
            + domain.strip()
            + "\n"
            + focus.strip()
            + "\n"
        )

        process.stdin.write(input_data)
        process.stdin.flush()
        process.stdin.close()

        status.update(
            label="Agent 正在研究，请稍候...",
            state="running"
        )

        # 实时读取 Agent 日志
        for line in iter(process.stdout.readline, ""):

            if not line:
                break

            logs.append(line.rstrip())

            # 防止页面一次渲染几万行
            visible_logs = logs[-250:]

            log_box.code(
                "\n".join(visible_logs),
                language=None
            )

        process.stdout.close()

        return_code = process.wait()

        if return_code != 0:

            status.update(
                label=f"Agent 运行失败，退出码：{return_code}",
                state="error"
            )

            st.error(
                "Agent 没有正常完成。"
                "请把上方最后约 30 行日志截图发给我。"
            )

            st.stop()

        status.update(
            label="✅ Agent 研究完成",
            state="complete"
        )

        # -------------------------------
        # 找这一次生成的最新报告
        # -------------------------------

        reports_dir = Path("reports")

        report_files = []

        if reports_dir.exists():

            for p in reports_dir.glob("*_report.md"):

                try:

                    if p.stat().st_mtime >= start_time - 5:
                        report_files.append(p)

                except OSError:
                    pass

        if report_files:

            report_path = max(
                report_files,
                key=lambda p: p.stat().st_mtime
            )

            report_text = report_path.read_text(
                encoding="utf-8"
            )

            st.divider()

            st.subheader("📊 最终竞品研究报告")

            st.markdown(report_text)

            st.download_button(
                "⬇️ 下载研究报告",
                data=report_text,
                file_name=report_path.name,
                mime="text/markdown",
                use_container_width=True
            )

            st.caption(
                f"报告文件：{report_path}"
            )

        else:

            st.warning(
                "Agent 已运行结束，但没有检测到本次生成的 *_report.md。"
            )

            st.info(
                "这通常意味着 Agent 在输出文件阶段发生了问题。"
                "把上方日志最后几十行发给我即可继续排查。"
            )

    except Exception as e:

        status.update(
            label="Agent 启动失败",
            state="error"
        )

        st.exception(e)
