# Search That

一个为 AstrBot 开发的强大番号搜索插件。

## 功能

-   通过 `/车牌号`  指令，根据番号搜索相关信息。
-   支持从多个有码和无码搜索引擎并发搜索。
-   自动提取搜索结果的封面图，并进行可配置的模糊（打码）处理。
-   返回一张封面图以及作品标题和页面链接。
-   高度可配置化，包括搜索引擎、搜索模式、代理、超时等。

## 使用

在支持的聊天平台发送：

-   `/车牌号 {番号}`

## 配置

插件支持通过 AstrBot 的管理面板进行详细配置，具体选项如下：

-   `search_mode`: 默认搜索模式（"全部", "仅有码", "仅无码"）。
-   `mosaic_reduce_first`: 在有码搜索中是否优先显示“无码破解”结果。
-   `censored_engines`: 有码搜索引擎列表。
-   `uncensored_engines`: 无码搜索引擎列表。
-   `error_keywords`: 结果标题中需要过滤掉的关键字。
-   `proxy`: 网络请求使用的代理地址，例如 `http://127.0.0.1:7890`。
-   `timeout`: 网络请求的超时时间（秒）。
-   `cover_mosaic_level`: 封面图的模糊（打码）程度，0为不处理。
-   `cover_regexes`: 用于从详情页提取封面图的正则表达式规则。
-   `return_details`: 是否在封面图下方附带标题和链接。

## 支持

-   [AstrBot 官网](https://astrbot.app)
-   [项目仓库](https://github.com/vmoranv/astrbot_plugin_search_that)
