"""Windows-friendly entry point: run, serve, or export."""
import argparse
import json
import getpass
from pathlib import Path

from agentradar.config import ROOT


def main():
    parser = argparse.ArgumentParser(description="AgentRadar · AI 技术雷达")
    parser.add_argument("command", choices=["run", "research", "tasks", "configure", "doctor", "feedback", "practice", "serve", "export"], nargs="?", default="run")
    parser.add_argument("question", nargs="?", help="research 的研究问题；feedback 的条目 ID")
    parser.add_argument("action", nargs="?", choices=["useful", "saved", "hard", "ignore", "completed"], help="feedback 的反馈类型")
    parser.add_argument("--resume", help="恢复已有研究任务 ID")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "configure":
        from dotenv import set_key
        from agentradar.config import load_config, validate_endpoint
        old = load_config(root)["llm"]
        print("配置保存在本机 .env，不会打印密钥或提交到 Git。")
        print("模型会接收研究问题、公开候选资料和必要偏好。")
        address = input("OpenAI 兼容接口地址（含 /v1）：").strip().rstrip("/") or old.get("base_url", "")
        model = input("模型名称：").strip() or old.get("model", "")
        validate_endpoint(address)
        key = getpass.getpass("API Key（输入隐藏；同一服务可留空保留）：").strip()
        if not address or not model or (not key and (not old.get("api_key") or address != old.get("base_url", "").rstrip("/"))):
            parser.error("必须填写接口、模型和对应密钥；未保存任何配置。")
        env = str(root / ".env")
        set_key(env, "RADAR_BASE_URL", address)
        set_key(env, "RADAR_MODEL", model)
        if key:
            set_key(env, "RADAR_API_KEY", key)
        print("配置已保存。运行 research 可开始自主研究，run 可生成日报。")
    elif args.command == "doctor":
        from agentradar.config import load_config, model_ready
        from agentradar.storage import Store
        import sys
        config = load_config(root)
        print("Python:", sys.version.split()[0])
        print("项目:", root)
        print("模型配置:", "完整（尚需真实连接验证）" if model_ready(config) else "缺少配置，请运行 configure")
        print("工具调用协议: Chat Completions 原生 function calling")
        print("每日模型调用上限:", config["max_llm_calls"])
        print("数据:", json.dumps(Store(root / "data/radar.db").stats(), ensure_ascii=False))
    elif args.command == "research":
        from agentradar.agent import research
        from agentradar.analysis import ModelFailure
        from agentradar.pipeline import AlreadyRunning
        if not args.question and not args.resume:
            parser.error('请提供研究问题，例如 research "最近哪些 Agent 记忆方案值得实践？"')
        def progress(event):
            label = {"tool_call": "调用工具", "tool_result": "工具返回", "finished": "研究完成",
                     "paused": "已保存进度", "validation_error": "参数或证据需修正", "invalid_output": "结果需修正", "model_error": "模型请求失败"}.get(event["type"], event["type"])
            print(label, event.get("tool", ""), event.get("status", ""), flush=True)
        try:
            state = research(root, args.question, args.resume, progress=progress)
        except (ModelFailure, AlreadyRunning, ValueError) as exc:
            print(str(exc))
            raise SystemExit(2)
        print("任务 ID:", state["id"], "状态:", state["status"])
        print("报告:", root / "reports/research" / (state["id"] + ".md"))
        if state["result"]:
            print(state["result"]["summary"])
        if state["error"]:
            print(state["error"])
            print("恢复命令: python radar.py research --resume", state["id"])
        raise SystemExit(0 if state["status"] == "completed" else 1)
    elif args.command == "tasks":
        from agentradar.storage import Store
        from agentradar.agent import TaskJournal
        print(json.dumps(TaskJournal(Store(root / "data/radar.db").path).recent(), ensure_ascii=False, indent=2))
    elif args.command == "feedback":
        from agentradar.storage import Store
        if not args.question or not args.action:
            parser.error("用法：feedback 条目ID useful|saved|hard|ignore|completed")
        try:
            Store(root / "data/radar.db").feedback(args.question, args.action)
        except ValueError:
            parser.error("条目 ID 或反馈类型无效")
        print("已保存反馈。")
    elif args.command == "practice":
        from agentradar.storage import Store
        from agentradar.pipeline import practice_for
        value = practice_for(Store(root / "data/radar.db"))
        print(json.dumps(value, ensure_ascii=False, indent=2) if value else "先把一条日报信息标记为 useful，再生成本周实践。")
    elif args.command == "serve":
        import uvicorn
        from agentradar.web import create_app
        uvicorn.run(create_app(root), host="127.0.0.1", port=args.port, log_level="warning")
    elif args.command == "export":
        from agentradar.storage import Store
        from agentradar.reports import export_report
        report = Store(root / "data/radar.db").report()
        if not report:
            parser.error("尚无日报，请先运行 run")
        export_report(root, report)
    else:
        from agentradar.pipeline import run, AlreadyRunning
        try:
            report, code = run(root)
        except AlreadyRunning:
            print("已有任务正在运行，本次跳过。")
            return
        print(json.dumps({"date": report["date"], "collected": report["collected"],
                          "selected": len(report["cards"]), "mode": report["mode"],
                          "sources": [{"source": s["source"], "status": s["status"]} for s in report["statuses"]]}, ensure_ascii=True))
        raise SystemExit(code)


if __name__ == "__main__":
    main()
