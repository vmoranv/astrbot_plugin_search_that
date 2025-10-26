import re
import asyncio
import aiohttp
import base64
from io import BytesIO
from typing import List, Dict, Any, Optional
from PIL import Image, ImageFilter

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

@register(
    "search_that",
    "vmoranv",
    "车牌号寻血猎犬",
    "1.0.0",
    "https://github.com/vmoranv/astrbot_plugin_search_that"
)
class SearchThatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.http_client: Optional[aiohttp.ClientSession] = None
        # 在插件初始化时调用 initialize
        asyncio.create_task(self.initialize())

    async def initialize(self):
        """初始化 aiohttp 客户端。"""
        proxy = self.config.get("proxy")
        self.http_client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.get("timeout", 10)),
            connector=aiohttp.TCPConnector(ssl=False) if not proxy else None,
        )
        logger.info("SearchThat 插件已加载，aiohttp 客户端已初始化。")

    async def terminate(self):
        """关闭 aiohttp 客户端。"""
        if self.http_client and not self.http_client.closed:
            await self.http_client.close()
            logger.info("aiohttp 客户端已关闭。")

    @filter.command("车牌号", alias={"番号"})
    async def search_handler(self, event: AstrMessageEvent, *, keyword: str):
        """
        主搜索指令，处理用户输入的番号。
        """
        if not keyword:
            yield event.plain_result("请输入要搜索的番号。")
            return

        code = self._separate(keyword)
        if not code:
            yield event.plain_result(f"在 “{keyword}” 中未找到有效的番号格式。")
            return

        search_mode = self.config.get("search_mode", "全部")
        yield event.plain_result(f"正在为 “{code}” 搜索({search_mode})结果...")

        cen_results, unc_results = [], []
        
        if search_mode == "全部":
            cen_task = asyncio.create_task(self._search_worker(code, "cen"))
            unc_task = asyncio.create_task(self._search_worker(code, "unc"))
            cen_results, unc_results = await asyncio.gather(cen_task, unc_task)
        elif search_mode == "仅有码":
            cen_results = await self._search_worker(code, "cen")
        elif search_mode == "仅无码":
            unc_results = await self._search_worker(code, "unc")

        # 过滤包含错误关键字的结果
        error_keywords = self.config.get("error_keywords", [])
        
        def filter_results(results):
            if not error_keywords:
                return results
            return [res for res in results if not any(kw in res['title'] for kw in error_keywords)]

        cen_results = filter_results(cen_results)
        unc_results = filter_results(unc_results)

        # 处理并发送最终结果
        all_results = cen_results + unc_results
        if not all_results:
            google_url = f"https://www.google.com/search?q={code}%20jav"
            yield event.chain_result([
                Comp.Plain("所有引擎均未找到结果, 你可以尝试: \n"),
                Comp.Plain(f"Google 搜索: {google_url}")
            ])
            return

        # 选择第一个结果作为代表
        main_result = all_results[0]
        title = main_result.get('title', 'N/A')
        page_url = main_result.get('url')

        cover_image = None
        if page_url and self.http_client:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
                }
                async with self.http_client.get(page_url, headers=headers, proxy=self.config.get("proxy")) as response:
                    if response.status == 200:
                        detail_html = await response.text()
                        cover_url = self._get_cover_url_from_html(page_url, detail_html)
                        if cover_url:
                            cover_image = await self._get_cover_image(cover_url)
            except Exception as e:
                logger.error(f"获取详情页或封面图失败 ({page_url}): {e}")

        # 构建最终消息
        final_chain = []
        if cover_image:
            # 使用 base64 协议头来传递图片数据
            final_chain.append(Comp.Image(file=f"base64://{cover_image}"))
        
        # 根据配置决定是否添加详细信息
        if self.config.get("return_details", True):
            final_chain.append(Comp.Plain(f"{title}\n{page_url}"))
        
        if not final_chain:
             yield event.plain_result(f"未找到 “{code}” 的相关结果，或配置为不返回任何信息。")
        else:
            yield event.chain_result(final_chain)


    def _separate(self, text_content: str) -> Optional[str]:
        """
        从文本中提取番号。
        """
        if not text_content:
            return None

        text_content = text_content.replace("—", "-")
        
        matchers = [
            re.compile(r"(?<![a-z0-9])[0-9]{5,}[_-][0-9]{2,5}(?![a-z0-9])", re.IGNORECASE),
            re.compile(r"((?<![a-z0-9])([0-9]*[a-z]+|[a-z]+[0-9]+[a-z]*))[_-]([a-z]*[0-9]{2,5}(?![0-9]))", re.IGNORECASE),
            re.compile(r"(?<![a-z0-9])[a-z]+[0-9]{3,}(?![a-z0-9])", re.IGNORECASE),
            re.compile(r"(?<![a-z0-9])[0-9]{4,}(?![a-z0-9-_])", re.IGNORECASE)
        ]

        for matcher in matchers:
            match = matcher.search(text_content)
            if match:
                return match.group(0)

        return None

    async def _search_worker(self, code: str, search_type: str) -> List[Dict[str, str]]:
        """
        根据类型（有码/无码）执行搜索。
        """
        if search_type == "cen":
            engine_urls = self.config.get("censored_engines", [])
        else:
            engine_urls = self.config.get("uncensored_engines", [])

        tasks = [self._crawl(url_str, code, search_type) for url_str in engine_urls]
        
        results_nested = await asyncio.gather(*tasks)
        
        all_results = [item for sublist in results_nested for item in sublist]

        if search_type == "cen" and self.config.get("mosaic_reduce_first", False):
            return sorted(all_results, key=lambda x: ('无码破解' not in x['title'], '中文字幕' not in x['title']))
        elif search_type == "cen":
            return sorted(all_results, key=lambda x: ('中文字幕' not in x['title'], '无码破解' not in x['title']))
        
        return all_results


    async def _crawl(self, url_str: str, code: str, search_type: str) -> List[Dict[str, str]]:
        """
        爬取单个搜索引擎。
        """
        if not self.http_client:
            return []

        parts = url_str.split("#POST#")
        url = parts[0]
        method = "POST" if len(parts) > 1 else "GET"
        payload_template = parts[1] if len(parts) > 1 else ""

        url = url.replace("%s", code)
        payload = payload_template.replace("%s", code) if payload_template else None
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        }
        
        engine_name = url.split('/')[2]
        try:
            logger.debug(f"正在请求: {url} (Method: {method})")
            if method == "POST":
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                async with self.http_client.post(url, data=payload, headers=headers, proxy=self.config.get("proxy")) as response:
                    text = await response.text()
                    final_url = str(response.url)
            else:
                async with self.http_client.get(url, headers=headers, proxy=self.config.get("proxy")) as response:
                    text = await response.text()
                    final_url = str(response.url)

            return self._parse_html(final_url, text, code, search_type)

        except Exception as e:
            logger.error(f"请求 {engine_name} ({url}) 失败: {e}")
            return []

    def _parse_html(self, final_url: str, html: str, code: str, search_type: str) -> List[Dict[str, str]]:
        """
        解析HTML以提取结果。
        """
        results = []
        _code_re = re.escape(code)

        try:
            if "7mmtv.sx" in final_url:
                final_origin = "https://7mmtv.sx"
                url_reg = re.compile(rf'"({re.escape(final_origin)}/zh/[^/]+_content/[^/]*/[^"]*{_code_re}[^"]*\.html)">([^<]*)</a>', re.IGNORECASE)
                for match in url_reg.finditer(html):
                    page_url, title = match.groups()
                    title = title.strip()
                    if search_type == "cen":
                        if "chinese" in page_url:
                            title = f"[中字] {title}"
                        if "reducing" in page_url:
                            title = f"[破解] {title}"
                    results.append({"title": title, "url": page_url})

            elif "supjav.com" in final_url:
                final_origin = "https://supjav.com"
                reg = re.compile(rf'"({re.escape(final_origin)}/zh/[0-9]+\.html)" title="([^"]*{_code_re}[^"]*)"', re.IGNORECASE)
                for match in reg.finditer(html):
                    page_url, title = match.groups()
                    title = title.strip()
                    if search_type == "cen":
                        if "无码破解" in title or "无码流出" in title or "無修正" in title:
                            title = f"[破解] {title}"
                        if "中文字幕" in title:
                             title = f"[中字] {title}"
                    results.append({"title": title, "url": page_url})
            
            elif "missav.ai" in final_url:
                reg = re.compile(rf'<a[^>]*href="([^"]*)"[^>]*>\s*<div class="my-2[^>]*>.*?<a[^>]*>([^<]*{_code_re}[^<]*)</a>', re.DOTALL | re.IGNORECASE)
                container_reg = re.compile(r'<div class="thumbnail group">([\s\S]+?)</div>\s*</div>', re.DOTALL)
                
                for container_html in container_reg.findall(html):
                    match = reg.search(container_html)
                    if match:
                        page_url, title = match.groups()
                        title = title.strip()
                        if search_type == "cen":
                            if "中文字幕" in container_html:
                                title = f"[中字] {title}"
                            if "无码影片" in container_html:
                                title = f"[破解] {title}"
                        results.append({"title": title, "url": page_url})

            elif "jable.tv" in final_url:
                reg = re.compile(rf'<a href="([^"]*)" title="([^"]*{_code_re}[^"]*)">', re.IGNORECASE)
                for match in reg.finditer(html):
                    page_url, title = match.groups()
                    results.append({"title": title.strip(), "url": page_url})

            elif "jav.guru" in final_url:
                reg = re.compile(rf'<a href="([^"]*{_code_re}[^"]*)">\s*<img[^>]*alt="([^"]*)"', re.IGNORECASE)
                for match in reg.finditer(html):
                    page_url, title = match.groups()
                    results.append({"title": title.strip(), "url": page_url})

            elif "123av.com" in final_url:
                final_origin = "https://123av.com"
                reg = re.compile(rf'<div class="detail">\s<a href="([^"]*{_code_re}[^"]*)">([^<]*)</a>', re.IGNORECASE)
                for match in reg.finditer(html):
                    page_url, title = match.groups()
                    results.append({"title": title.strip(), "url": final_origin + "/zh/" + page_url})
            
            elif "jav777.xyz" in final_url:
                if "?s=" in final_url:
                    reg = re.compile(r'post-title"><a href="([^"]*)"', re.IGNORECASE)
                    first_match = reg.search(html)
                    if first_match and self.http_client:
                        detail_url = first_match.group(1)
                        async def fetch_detail():
                            try:
                                async with self.http_client.get(detail_url, headers={"User-Agent": "Mozilla/5.0"}, proxy=self.config.get("proxy")) as resp:
                                    detail_html = await resp.text()
                                    if f"【番號】︰{code}" in detail_html:
                                        return [{"title": f"[中字] {code} (jav777)", "url": detail_url}]
                            except Exception:
                                return []
                            return []
                        return asyncio.run_coroutine_threadsafe(fetch_detail(), asyncio.get_event_loop()).result()
                
        except Exception as e:
            logger.error(f"解析 {final_url} 出错: {e}")
        
        return results[:5]

    def _get_cover_url_from_html(self, url: str, html: str) -> Optional[str]:
        """从HTML中提取封面URL"""
        current_domain = url.split('/')[2]
        regex_rules = self.config.get("cover_regexes", [])
        
        for rule_item in regex_rules:
            domain = None
            regex = None
            
            # Handle new string format: "domain|regex"
            if isinstance(rule_item, str):
                parts = rule_item.split('|', 1)
                if len(parts) == 2:
                    domain, regex = parts
            # Handle old dict format: {"domain": "...", "regex": "..."}
            elif isinstance(rule_item, dict):
                domain = rule_item.get("domain")
                regex = rule_item.get("regex")

            if domain == current_domain and regex:
                match = re.search(regex, html)
                if match:
                    cover_url = match.group(1)
                    if not cover_url.startswith("http"):
                        cover_url = "https://" + current_domain + cover_url
                    return cover_url
        return None

    async def _get_cover_image(self, url: str) -> Optional[str]:
        """下载、处理并返回 base64 编码的封面图片"""
        if not self.http_client:
            return None
        try:
            async with self.http_client.get(url, proxy=self.config.get("proxy")) as response:
                if response.status == 200:
                    image_data = await response.read()
                    mosaic_level = self.config.get("cover_mosaic_level", 0.3)
                    
                    processed_image_bytes = image_data
                    if mosaic_level > 0:
                        with Image.open(BytesIO(image_data)) as img:
                            w, h = img.size
                            radius = int(max(w, h) * mosaic_level / 10)
                            if radius > 0:
                                img = img.filter(ImageFilter.GaussianBlur(radius=radius))
                            
                            buffered = BytesIO()
                            img.save(buffered, format="JPEG")
                            processed_image_bytes = buffered.getvalue()
                    
                    return base64.b64encode(processed_image_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"下载或处理封面图失败: {e}")
        return None
