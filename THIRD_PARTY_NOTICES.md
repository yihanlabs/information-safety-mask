# 第三方组件与素材说明

本项目原创代码及文档采用根目录 [MIT 许可证](LICENSE)，版权为 © 2026 yihanlabs。第三方组件、模型、图标、字体和制作工具保留各自的版权及许可证，不因本项目采用 MIT 而改变。

## 运行组件

下表记录 v0.1.1 使用的核心组件。精确依赖图以 `uv.lock` 和前端锁文件为准；安装包中的 LICENSE、NOTICE、第三方许可文件及上游发布文件为对应组件的原始声明。本仓库源码包不附带 Python 环境、前端依赖、原生库或模型权重，安装阶段从原有来源下载。

| 组件 | 用途 | 上游许可与来源 |
| --- | --- | --- |
| PaddleOCR 3.3.2 | 中文文字检测和识别 | [Apache-2.0](https://github.com/PaddlePaddle/PaddleOCR/blob/v3.3.2/LICENSE) |
| PaddlePaddle 3.2.2 | OCR 推理 | [Apache-2.0](https://github.com/PaddlePaddle/Paddle/blob/v3.2.2/LICENSE) |
| PaddleX 3.3.13 | OCR 模型管线 | [Apache-2.0](https://github.com/PaddlePaddle/PaddleX/blob/v3.3.13/LICENSE) |
| spaCy 3.8.14 | 中文实体分析 | [MIT](https://github.com/explosion/spaCy/blob/master/LICENSE) |
| PyTorch 2.14.0 | 实体模型推理 | [上游 LICENSE 与随包第三方声明](https://github.com/pytorch/pytorch/blob/main/LICENSE)；该锁定发行包的声明包含 Apache-2.0、LLVM exception、BSD、BSL-1.0 和 MIT |
| FastAPI / Uvicorn | 本地 HTTP 服务 | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) / [BSD-3-Clause](https://github.com/encode/uvicorn/blob/main/LICENSE.md) |
| Pillow 12.3.0 | 图像与合成素材 | [MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| pyvips 3.1.0 | libvips Python 绑定 | [MIT](https://github.com/libvips/pyvips/blob/master/LICENSE.txt) |
| libvips | 顺序解码、区域读取 | [LGPL-2.1-or-later](https://www.libvips.org/) |
| pyvips-binary 8.18.6 | 预编译图像库发行包 | 该发行包元数据声明为 [LGPL-3.0-or-later](https://pypi.org/project/pyvips-binary/8.18.6/)，与 libvips 核心库的许可声明分别保留 |
| cryptography 50.0.1 | AES-GCM 临时缓存 | [Apache-2.0 OR BSD-3-Clause](https://github.com/pyca/cryptography/blob/main/LICENSE) |
| psutil 7.2.2 | 资源状态读取 | [BSD-3-Clause](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| NumPy / OpenCV | 像素与几何计算 | [NumPy 随包许可](https://github.com/numpy/numpy/blob/main/LICENSE.txt) / [OpenCV Apache-2.0 及第三方声明](https://github.com/opencv/opencv-python/blob/master/LICENSE.txt) |
| React / React DOM / React Konva / Konva | 编辑界面与画布 | [React MIT](https://github.com/facebook/react/blob/main/LICENSE)、[React Konva MIT](https://github.com/konvajs/react-konva/blob/master/LICENSE)、[Konva MIT](https://github.com/konvajs/konva/blob/master/LICENSE) |
| Lucide React 0.468.0 | 界面图标 | [ISC；部分 Feather 来源保留 MIT 声明](https://github.com/lucide-icons/lucide/blob/main/LICENSE) |

libvips、pyvips-binary 及其包含的编解码组件不以本项目 MIT 重新授权。如另行制作包含这些二进制的安装包，应保留对应版权、许可、源码获取等要求；当前 Release 仅提供本项目源码和展示素材。

## 模型

| 模型 | 来源与许可证 |
| --- | --- |
| PP-OCRv5_server_det | [PaddlePaddle 官方模型卡，Apache-2.0](https://huggingface.co/PaddlePaddle/PP-OCRv5_server_det) |
| PP-OCRv5_server_rec | [PaddlePaddle 官方模型卡，Apache-2.0](https://huggingface.co/PaddlePaddle/PP-OCRv5_server_rec) |
| zh_core_web_trf 3.8.0 | [Explosion 官方模型，MIT](https://spacy.io/models/zh#zh_core_web_trf)；同时保留模型包中的 `LICENSES_SOURCES` |

模型包中的来源说明包含 OntoNotes 5（由 Explosion 获得许可）、CoreNLP 转换器的引用和 bert-base-chinese。模型 MIT 声明不等同于授予训练语料的获取或再分发权；本项目不提供这些训练语料。模型文件哈希继续使用既有 `models.lock.json`，此次未替换模型。

## 演示与制作工具

- 截图和录屏来自实际工作台，表单与长图均为代码生成的虚构内容；不包含用户文件。演示经过剪辑，不用作 OCR 速度或准确率证明。
- 字体调用 Windows 中已有的 Microsoft YaHei；中文旁白调用已安装的 Microsoft Huihui。仓库和 Release 不分发字体文件或语音引擎。
- 封面由本仓库 HTML/CSS 排版，图形为本项目绘制的盾牌；保留实际截图中的第三方界面图标声明。
- 演示使用 Playwright（Apache-2.0）驱动独立浏览器，FFmpeg 进行视频编码；不附带浏览器或 FFmpeg 二进制。Vite（MIT）、TypeScript（Apache-2.0）为构建工具，原始许可随相应安装包提供。

这些说明覆盖核心组件和公开展示素材，不替代所有传递依赖各自的许可文件。
