# zqhd-travel-map

**咱去换电 · 旅行地图**：上传全部换电订单，按订单时间与真实站点位置生成全屏行程轨迹，以及可叠加到实拍视频的透明地图图层。

支持手动设置起点、出发日期时间、终点、到达日期时间和成片总长度。默认竖屏1080×1920、15秒、30fps；全屏与透明版沿用已确认的交付UI。

## 安装到Codex

```bash
git clone https://github.com/renqix/zqhd-travel-map.git ~/.codex/skills/zqhd-travel-map
python3 -m pip install -r ~/.codex/skills/zqhd-travel-map/requirements.txt
```

还需要本机FFmpeg和中文字体，例如Noto Sans CJK。macOS可通过Homebrew安装FFmpeg，Linux可使用系统包管理器；字体也可通过`--font`、`--bold-font`或`TRAVEL_MAP_FONT`、`TRAVEL_MAP_BOLD_FONT`指定。`--ffmpeg`可指定可执行文件。

在Codex中上传订单并调用：

> 使用 $zqhd-travel-map，读取我上传的全部换电订单。起点为我指定的地点，出发时间为完整日期时间，终点为我指定的地点，到达时间为完整日期时间。视频总长15秒，同时输出全屏轨迹和右上角透明地图。

首次安装后，在新对话中调用。技能会读取截图里的**下单时间、换电站名称、实际新增电量**，核对跨日顺序、重复订单和同名站点。也可以直接上传已经整理好的订单表。若地点或日期不完整，会先补齐必要信息。

## 两种视频

| 输出 | 内容与用途 |
| --- | --- |
| `fullscreen.mp4` | 9:16俯视地图，沿道路行驶。左上四行两列对齐，显示累计估算里程、行程用时、累计补充电量、换电次数。 |
| `corner-canvas-alpha.mov` | 1080×1920全画布定位版，ProRes4444带Alpha，可直接放在实拍视频上方。 |
| `corner-alpha.mov` | 540×540紧凑版透明图层；以原尺寸放到画布右上角。 |
| `corner-preview.mp4` | 用示意背景预览透明地图；实际透明MOV不含背景。 |
| `previews/`、`manifest.json`、`timeline.json` | 关键帧、数据核验与到站时间，便于检查和调整。 |

右上地图默认圆心在画布右上顶点、半径540像素，向画布内呈四分之一圆域；外缘约108像素平滑渐隐并柔化。全程完整地图朝北，不加额外仪表和说明文字。小车到站后才出现本站名称、上一地点到本站的区间公里和该订单新增电量，随后保留；未来站点只显示打点。到站停留短，主要时长用于行驶轨迹。

实际样式约定见[references/design.md](references/design.md)。根据具体订单数量和站名长度重新排标签，布局容量不足会明确报错，不静默省略订单。

## 脚本方式

复制[assets/trip.example.json](assets/trip.example.json)到自己的任务目录，换成核对过的站点、WGS84坐标、订单时间和电量。示例站名、电量和日期是虚构教学数据，不能当真实换电订单交付。

```bash
python3 scripts/prepare_trip.py --input /任务目录/trip.json --output /任务目录/prepared \
  --start-name '我的起点' --start-time '2026-01-01T09:00:00' \
  --start-lon 121.5 --start-lat 31.2 --duration 15

python3 scripts/render_video.py --prepared /任务目录/prepared/prepared.json \
  --output /任务目录/videos --mode all

python3 scripts/verify_outputs.py --manifest /任务目录/videos/manifest.json
```

准备阶段设置的尺寸、半径和时长会传给绘图器，绘图命令的显式参数可再次覆盖。角落图层半径至少320像素，默认540；文字与订单多时应使用更大的半径。`--mode fullscreen`或`--mode overlay`可分别输出；`--preview-only`先看关键帧；重复写入同一视频目录需要明确传`--overwrite`。

`lookup_places.py`返回地名候选供人工核对，不自动挑第一条。`prepare_trip.py`通过OSRM取得分段道路几何与距离，通过默认Esri底图拼接完整地图；支持`--route-file`、`--basemap-json`复用已有道路与底图缓存。替换服务可用`--osrm-base`和`--tile-url`，同时用`--attribution`填写实际底图与道路的来源署名，完整参数请查看各脚本`--help`。两种本地缓存齐备时可离线重做。

## 数据口径

- 排序以订单完整日期时间为准，不用手机状态栏时间和上传顺序。
- “换电度数”使用订单的实际**新增电量**；最低20度计费提示不改变实际电量。
- 订单新增电量合计是**累计补充电量**。若没有车辆耗电记录，不能把它写成实际消耗电量。
- 没有原始GPS时，道路按核实站点重建，距离属于道路估算；事件之间的时钟和车位经过插值，不能证明真实中途分岔或逐秒车速。
- 未指定终点时，明确以最后换电站和订单时间结束，保留末次换电事件。
- 用户订单、车牌、订单号、截图、地图缓存和导出视频留在任务目录。仓库只含通用代码与虚构示例。

坐标、时间、缓存格式详见[references/data-format.md](references/data-format.md)。地图数据保留来源署名，并遵守所选服务的使用条件。代码采用MIT许可，第三方地图、字体与服务各自遵守其许可。

## 核验

```bash
python3 -m unittest discover -s tests -v
```

单元检查涵盖跨日排序、手动覆盖、重复订单、电量口径、时间冲突和道路端点。导出验收还会实际解码视频，核对帧数/尺寸/时长、Alpha圆外透明和渐隐，以及到站标签在到站前后帧的显现。自动检查后仍应观看关键帧，检查地图走向与文字可读性。
