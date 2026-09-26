"""Isolated, real-model session for the public demonstration (never user images)."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "test-results" / "promo-v0.1.1"
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "scripts")]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "session.json").exists():
        raise RuntimeError("演示会话文件已存在，请先结束上一轮演示会话，不要覆盖其令牌")
    os.environ["LOCALAPPDATA"] = str(OUT / "appdata")
    import uvicorn
    from conftest import MemoryRepository
    from PIL import Image, ImageDraw, ImageFont
    from verify_offline import fixture

    from safety_mask.api import create_app
    from safety_mask.cache import SessionCache
    from safety_mask.models import CustomField, RuleSet
    from safety_mask.network import install_local_only_guard
    from safety_mask.state import SessionStore
    from safety_mask.worker import ProcessEngine

    ordinary, targets = fixture()
    ordinary.save(OUT / "form.png")
    ordinary.close()
    tall = Image.new("RGB", (1100, 4200), "#f4f7f5")
    draw = ImageDraw.Draw(tall)
    font = lambda size: ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)
    sections = [
        (
            "01 · 申请资料",
            [
                "此长图为合成演示，不对应真实个人或业务。",
                "项目类型：本地文档整理",
                "处理方式：逐段检查，确认后导出。",
            ],
        ),
        ("02 · 联系信息", ["姓名：张三", "电话：13800138000", "地址：北京市海淀区示例路18号302室"]),
        (
            "03 · 内部备注与附件",
            ["申请事项：项目资料归档", "内部编号：DEMO-NOTE-42", "备注说明：附件中的内部编号需人工检查。"],
        ),
        (
            "04 · 办理记录",
            ["资料状态：已整理", "检查项目：正文、附件与补充说明", "下一步：逐张检查已遮盖区域。"],
        ),
        (
            "05 · 确认信息",
            [
                "文件用途：界面功能演示",
                "确认方式：检查图片后由用户确认",
                "导出格式：每张图片独立保存为 PNG。",
            ],
        ),
        (
            "06 · 文档结束",
            ["此处是长截图的末尾。", "示例内容仅用于展示滚动、补框与定位。", "请检查整张图片后再导出。"],
        ),
    ]
    for i, (title, rows) in enumerate(sections):
        top = 40 + i * 690
        draw.rounded_rectangle(
            (40, top, 1060, top + 640), radius=12, fill="white", outline="#dbe5de", width=2
        )
        draw.rectangle((40, top, 1060, top + 6), fill="#176d57")
        draw.text((85, top + 48), title, font=font(34), fill="#234637")
        draw.text((85, top + 108), "合成示例 · 长图检查", font=font(20), fill="#7f9587")
        for j, row in enumerate(rows):
            draw.text((85, top + 210 + j * 100), row, font=font(28), fill="#263e31")
        draw.text((85, top + 574), f"第 {i + 1} 节 / 共 6 节", font=font(19), fill="#879d8d")
    tall.save(OUT / "long.png")
    tall.close()
    (OUT / "targets.json").write_text(json.dumps(targets, ensure_ascii=False), encoding="utf8")

    install_local_only_guard()
    store = SessionStore(
        ProcessEngine(memory_saving=True), MemoryRepository(), cache=SessionCache(OUT / "cache")
    )
    store.update_rules(RuleSet(custom_fields=[CustomField(id="demo-project", value="ALPHA-42")]))
    token = secrets.token_urlsafe(32)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    app = create_app(token, store)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="critical")
    )

    @app.post("/api/demo-stop")
    def stop():
        server.should_exit = True
        return {"stopping": True}

    (OUT / "session.json").write_text(json.dumps({"port": port, "token": token}), encoding="utf8")
    try:
        server.run(sockets=[listener])
    finally:
        store.close()
        (OUT / "session.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
