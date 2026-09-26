# v0.1.1 展示素材

演示仅使用合成登记表和长图，真实加载既有 PP-OCRv5 / spaCy 模型。在独立会话中完成导入、自动遮盖、手动补框、确认及两张 PNG 下载。片中明确标注识别等待已剪辑，不代表实际识别耗时。

录屏中的绿色光标圆点用于显示鼠标位置，不拦截输入，也不修改应用状态。

- `narration.json`：五段中文旁白原文，Microsoft Huihui 合成，无背景音乐。
- `yinqu-demo.zh-CN.srt`：与成片一致的字幕，视频中也已烧录。
- 成片 MP4 从 [v0.1.1 Release](https://github.com/yihanlabs/information-safety-mask/releases/tag/v0.1.1) 获取，避免把原始录像写入 Git 历史。
- 分享封面位于 `docs/images/social-preview.png`，HTML/CSS 来源为 `scripts/promo/artwork.html`。

## 复现

需要先完成项目安装与模型准备，并具备本机 Edge、FFmpeg（libx264、AAC、libass）和 Microsoft Huihui 中文声音。素材脚本不是应用启动流程的一部分，不新增运行依赖。

在项目根目录执行：

1. 使用 `.venv-cpu/Scripts/python.exe scripts/promo/demo_server.py` 启动独立演示会话。
2. 在另一个终端运行 `node scripts/promo/capture.cjs`，等待真实识别与录制结束；脚本只关闭自己创建的演示会话。
3. 运行 `node scripts/promo/render_art.cjs` 生成封面和片头、片尾。
4. 运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/promo/narrate.ps1` 生成五段本地配音。
5. 运行 `.venv-cpu/Scripts/python.exe scripts/promo/assemble.py` 合成 32.5 秒 MP4 和字幕。
6. 运行 `.venv-cpu/Scripts/python.exe scripts/promo/verify.py` 检查实际 PNG 遮盖笔画、原图尺寸、元数据、会话退出和成片规格。

中间数据保存在被 Git 忽略的 `test-results/promo-v0.1.1/`：随机会话令牌、合成原图、原始录屏、合成样例识别验证记录及导出 PNG 不提交。性能选择及缓存使用该目录内独立的用户数据根目录，不触碰用户正在运行的其他会话。请串行运行，已有演示会话时不要再次启动。

## 本次素材验收

真实 OCR 得到普通表单 13 行、长图 36 行。首轮像素检查发现长图手机号自动框边缘遗漏 3 个笔画像素，演示增加了在实际界面中扩展自动框的操作；同时给内部编号手动补框。重新录制后，两张均已检查、确认并独立下载，表单 5 项、长图 4 项已知敏感内容的笔画全部覆盖。导出保持 1100 × 680、1100 × 4200 的原始尺寸、RGB 和无原始元数据，独立演示会话已退出。这些数值只验证演示样例，不代表真实场景准确率。

成片为 32.5 秒、1920 × 1080、30fps、H.264/AAC，1,352,885 字节；封面为 1280 × 640 RGB PNG，199,030 字节，无图片元数据。验收以每秒两帧覆盖整段画面，并核对字幕内容和时间轴。音轨检查包括完整解码、非静音、音量峰值和本机中文识别回读；未进行人工试听，自动回读存在同音识别误差。

## 分享封面设置

仓库管理员可在 [Settings → General](https://github.com/yihanlabs/information-safety-mask/settings) 的 **Social preview → Edit → Upload an image** 上传封面。图片已经制作并不等于远端设置完成，应在上传后检查 GitHub 实际显示的分享图。
