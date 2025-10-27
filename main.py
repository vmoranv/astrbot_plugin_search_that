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

    @filter.command("女优")
    async def actress_search_handler(self, event: AstrMessageEvent, *, name: str):
        """
        根据女优姓名搜索其个人信息。
        """
        if not name:
            yield event.plain_result("请输入要搜索的女优姓名。")
            return

        yield event.plain_result(f"正在查找「{name}」的信息...")

        # 并行查询所有数据源
        try:
            tasks = [
                self._fetch_av2ch_info(name),
                self._fetch_wikipedia_info(name),
                self._fetch_avwiki_info(name)
            ]
            results = await asyncio.gather(*tasks)

            # 合并结果
            final_info = {"year": None, "height": None, "measurements": None}
            for res in results:
                if res:
                    if res.get("year") and not final_info.get("year"):
                        final_info["year"] = res["year"]
                    if res.get("height") and not final_info.get("height"):
                        final_info["height"] = res["height"]
                    if res.get("measurements") and not final_info.get("measurements"):
                        final_info["measurements"] = res["measurements"]

            # 格式化并发送
            formatted_str = self._format_actress_info(
                final_info["year"], final_info["height"], final_info["measurements"]
            )

            if formatted_str:
                yield event.plain_result(f"「{name}」\n{formatted_str}")
            else:
                logger.info(f"所有数据源均未找到「{name}」的有效信息。")
                yield event.plain_result(f"未能找到关于「{name}」的有效信息。")

        except Exception as e:
            logger.error(f"查询女优 {name} 时发生错误: {e}")
            yield event.plain_result(f"查询「{name}」时发生错误，请检查后台日志。")

    def _format_actress_info(self, year: Optional[str], height: Optional[str], measurements: Optional[str]) -> Optional[str]:
        """格式化女优信息，如果信息不全则返回 None"""
        year = year or '?'
        height = height or '?'
        measurements = measurements or '?'

        # 只有在所有信息都未知时才返回 None
        if year == '?' and height == '?' and measurements == '?':
            return None

        if height.isdigit() and int(height) >= 168:
            height = f"⭐{height}"
        
        return f"[出生] {year}\n[身高] {height}\n[三围] {measurements}"

    async def _fetch_av2ch_info(self, name: str) -> Optional[Dict[str, str]]:
        """从 av2ch.net 获取信息"""
        if not self.http_client: return None
        try:
            url = 'https://av2ch.net/avsearch/avs.php'
            data = f"keyword={name}&gte_height=min&lte_height=max&gte_bust=min&lte_bust=max&gte_waist=min&lte_waist=max&gte_hip=min&lte_hip=max&gte_cup=min&lte_cup=max&gte_age=min&lte_age=max&genre_01=&genre_02="
            logger.info(f"AV2CH request URL: {url} with data keyword={name}")
            headers = {
                'Content-type': 'application/x-www-form-urlencoded',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'
            }
            async with self.http_client.post(url, data=data, headers=headers, proxy=self.config.get("proxy")) as response:
                if response.status != 200:
                    logger.warning(f"AV2CH request for {name} failed with status {response.status}")
                    return None
                html = await response.text()
                
                box_actress_re = re.compile(r'<div class="box_actress">(.*?)<div class="link_actress">', re.DOTALL)
                text_actress_html = None
                
                for match in box_actress_re.finditer(html):
                    box_html = match.group(1)
                    if f'<h2 class="h2_actress">{name}</h2>' in box_html:
                        text_match = re.search(r'<div class="text_actress">(.*?)</div>', box_html, re.DOTALL)
                        if text_match:
                            text_actress_html = text_match.group(1)
                            break
                
                if not text_actress_html:
                    return None

                year_match = re.search(r'(\d{4})年', text_actress_html)
                height_match = re.search(r'身長<b>(\d{3})</b>cm', text_actress_html)
                measure_match = re.search(r'[BＢ]([\d?]+)cm.*?[WＷ]([\d?]+)cm.*?[HＨ]([\d?]+)cm', text_actress_html)

                year = year_match.group(1) if year_match else None
                height = height_match.group(1) if height_match else None
                measurements = None
                if measure_match:
                    b, w, h = measure_match.groups()
                    if b != '?': # Only form measurements if not '?'
                        measurements = f"B{b}-W{w}-H{h}"

                logger.info(f"AV2CH Extracted: year={year}, height={height}, measurements={measurements}")
                return {"year": year, "height": height, "measurements": measurements}
        except Exception as e:
            logger.error(f"从 av2ch 获取 {name} 信息失败: {e}")
            return None

    async def _fetch_wikipedia_info(self, name: str) -> Optional[Dict[str, str]]:
        """从 ja.wikipedia.org 获取信息"""
        if not self.http_client: return None
        try:
            url = f"https://ja.wikipedia.org/wiki/{name}"
            logger.info(f"Wikipedia request URL: {url}")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'}
            async with self.http_client.get(url, headers=headers, proxy=self.config.get("proxy")) as response:
                if response.status != 200:
                    logger.warning(f"Wikipedia request for {name} failed with status {response.status}")
                    return None
                html = await response.text()

                year_match = re.search(r'生年月日.*?>.*?(\d{4})年', html, re.DOTALL)
                height_match = re.search(r'身長.*?>.*?(\d{3})\s*cm', html, re.DOTALL)
                measure_match = re.search(r'スリーサイズ.*?>.*?(\d{2,3})\s*-\s*(\d{2,3})\s*-\s*(\d{2,3})\s*cm', html, re.DOTALL)

                year = year_match.group(1) if year_match else None
                if year and int(year) < 1980:
                    return None
                height = height_match.group(1) if height_match else None
                measurements = None
                if measure_match:
                    measurements = f"B{measure_match.group(1)}-W{measure_match.group(2)}-H{measure_match.group(3)}"

                logger.info(f"Wikipedia Extracted: year={year}, height={height}, measurements={measurements}")
                return {"year": year, "height": height, "measurements": measurements}
        except Exception as e:
            logger.error(f"从 Wikipedia 获取 {name} 信息失败: {e}")
            return None

    async def _fetch_avwiki_info(self, name: str) -> Optional[Dict[str, str]]:
        """从 av-wiki.net 获取信息"""
        if not self.http_client: return None
        try:
            search_url = f"https://av-wiki.net/?s={name}&post_type=product"
            logger.info(f"AV-WIKI search URL: {search_url}")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'}
            detail_url = None
            async with self.http_client.get(search_url, headers=headers, proxy=self.config.get("proxy")) as response:
                html = await response.text()
                url_match = re.search(rf"<a href=['\"](https://av-wiki\.net/actress/[\w-]+/)['\"] rel=['\"]bookmark['\"]>{re.escape(name)}[^<]*</a>", html)
                if url_match:
                    detail_url = url_match.group(1)

            if not detail_url:
                return None

            logger.info(f"AV-WIKI detail URL: {detail_url}")
            async with self.http_client.get(detail_url, headers=headers, proxy=self.config.get("proxy")) as response:
                html = await response.text()
                
                info_text_match = re.search(r'<dl class="actress-data">(.*?)</dl>', html, re.DOTALL)
                if not info_text_match:
                    return None
                info_text = info_text_match.group(1)

                year_match = re.search(r'<dd>(\d{4})年', info_text)
                height_match = re.search(r'<dd>(\d{3})cm', info_text)
                measure_match = re.search(r'<dd>([B]\d{2,3}.*?[W]\d{2,3}.*?[H]\d{2,3})', info_text)

                year = year_match.group(1) if year_match else None
                height = height_match.group(1) if height_match else None
                measurements = None
                if measure_match:
                    clean_measure = re.sub(r'<[^>]+>', '', measure_match.group(1)).strip()
                    parts = re.findall(r'([BWH])(\d{2,3})', clean_measure)
                    if len(parts) == 3:
                        measurements = f"{parts[0][0]}{parts[0][1]}-{parts[1][0]}{parts[1][1]}-{parts[2][0]}{parts[2][1]}"
                
                logger.info(f"AV-WIKI Extracted: year={year}, height={height}, measurements={measurements}")
                return {"year": year, "height": height, "measurements": measurements}
        except Exception as e:
            logger.error(f"从 av-wiki 获取 {name} 信息失败: {e}")
            return None
