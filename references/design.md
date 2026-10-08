# 已确认的交付版UI

## 全屏轨迹

默认1080×1920、9:16、30fps、15秒，无配乐。沿真实道路连续推进，北向朝上。全程灰色参考线、已行驶青蓝轨迹、白描边位置箭头、地理锚定站点。

顶部白色圆角标题“咱去换电 · 加电之旅”和起终城市。左上深蓝面板：标签左对齐，数值右对齐，每行统一垂直中心；四行累计估算公里、经过时间、累计补充电量、换电次数。颜色参考深蓝 `#112735`、青色 `#0091A4`、进度黄 `#FFCD37`。

到站信息包括站名、订单日期时间、当次新增电量；行驶期间显示下一个地点。底部显示累计换电新增电量、从出发到到达的进度。全程时间包括休息、等待，不称纯驾驶时间。开头全景进入起点，结尾回看全程；停顿随成片总长等比例调整，短片到站约0.35秒。

## 右上角透明图层

圆心在画布右上顶点 `(width,0)`，默认半径为540像素（1080宽画布），范围向画布内延伸。不是把半圆中心移到画布中间：该圆在画布内露出左下四分之一。边缘向内用约20%半径的宽渐隐带，Alpha平滑到0；底图外缘细节同步柔化。主体浅色、路线蓝色 `#2172E6`，文字清楚，没有额外标题或仪表。

保持完整起终路径，9站示例的标签两行：服务区/收费站名称；`区间 111.8km · 27.1度`。区间从上一点计算，首段从手动起点；终点标最后一段公里。标签用细引线锚定真实打点，避开路线和羽化带。到站后首次显示并保留，未到时只显示打点。结束时所有已到站信息可见。地图顶边保留细小的底图来源署名，标签避开该区域。

MOV为ProRes4444，Alpha外部全透明，内部底图约76%不透明度，文字、路线清晰。提供全画布定位版与radius×radius紧凑版，另提供MP4叠加示意。示意背景不进入Alpha素材。

## 来源

道路接口：[OSRM Route](https://project-osrm.org/docs/v5.24.0/api/#route-service)。地图及道路位置不能替代原始GPS。

默认底图：[Esri World Topographic Map](https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer)。遵守所选服务的当前使用条件，不把瓦片作为技能资源公开分发。

署名按实际服务来源保留并显示在地图中，参考[Esri底图署名说明](https://support.esri.com/en-us/knowledge-base/what-is-the-correct-way-to-cite-an-arcgis-online-basema-000012040)。替换底图时同步更新`--attribution`。

地理数据署名：[OpenStreetMap contributors](https://www.openstreetmap.org/copyright)。来源记录及地图署名随交付保留。地名查询遵守[Nominatim使用政策](https://operations.osmfoundation.org/policies/nominatim/)，不自动选同名第一条。
