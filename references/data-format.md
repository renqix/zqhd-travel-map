# 订单和行程数据

`trip.json` 是标准输入。完整示例见 `assets/trip.example.json`；其中时间和电量是虚构教学数据。

```json
{
  "timezone": "Asia/Shanghai",
  "start": {"name": "出发地点", "time": "2026-01-01T09:00:00", "lon": 121.5, "lat": 31.2},
  "end": {"name": "到达地点", "time": "2026-01-01T13:00:00", "lon": 120.1, "lat": 30.2},
  "orders": [
    {"name": "某服务区", "time": "2026-01-01T10:00:00", "lon": 120.7, "lat": 30.6,
     "energy_added_kwh": 27.1, "location_source": "已核对的地图链接"}
  ]
}
```

日期时间包括日期，使用同一时区。坐标为WGS84，经度 `lon` 在前、纬度 `lat` 在后；来自高德等偏移坐标不能直接混用。`energy_added_kwh` 是“新增电量”，为有限、非负数；0是真实零值，无法读取时应标记缺失并补齐，不能填0充数。

可选字段：`order_id`用于识别同一订单的重复截图；`source_image`指向本地附件；`source_station_text`保留截断原文；`location_source`记录地图/公告来源。所有这些属于任务输入，技能仓库不需要用户的真实订单。

准备程序升序排序。相同订单号内容冲突会报错；明确重复项移除并记入数据说明。第一笔订单必须不早于手动出发时间，终点必须不早于最后订单。若缺少终点，默认以最后换电站结束、保留最后换电事件，不推测新的目的地。

相邻事件同时间只允许在同一坐标且道路距离为零。虚构订单须标记`example_only: true`，合成道路须标记`synthetic_test_only: true`，标记随准备数据与画面说明保留，不能把测试线路标为真实道路。

`prepared.json` 包含：

- `trip`：归一化行程和有序订单。
- `points`：起点、换电点、终点。
- `route.distance`：总道路估算米数；`route.legs[].distance`为上一点到下一点米数。
- `route.legs[].coordinates`：实际道路折线 `[lon,lat]`。
- `basemap`：拼图路径、世界像素原点与zoom，视野覆盖全屏和角落两种模式。
- `attribution`和`assumptions`：来源署名、道路重建/里程估算及数据精度说明。

`prepare_trip.py --route-file`可复用已有OSRM JSON或准备好的分段道路JSON；`--basemap-json`可引用已有地图描述。两者同时使用可离线重做。不能用合成直线文件作为真实地图的交付来源；测试夹具须明确标为合成。
