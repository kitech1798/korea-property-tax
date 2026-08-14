// 주소정보 누리집(juso.go.kr) 중계 — **서울 리전에서만 돈다.**
//
// 왜 필요한가
//   business.juso.go.kr 이 해외 IP에 응답하지 않는다(2026-08-13 실측:
//   한국 회선 0.3초 정상 / 미국 AWS 20초 타임아웃). 앱은 Streamlit Cloud(미국)에
//   있어서 주소 검색이 통째로 죽었다.
//
//   앱을 국내로 옮기는 대신 **중계만 서울에 둔다.** 지역 고정은 vercel.json 의
//   "regions": ["icn1"] 이 한다 — 이 파일이 아니라 거기에 있으니 함께 봐야 한다.
//
// 승인키는 여기에만 있다
//   앱은 승인키를 모른다. 검색어만 보내고, 키는 이 함수가 환경변수에서 붙인다.
//   그래서 키가 Streamlit 쪽에도, 브라우저에도, 저장소에도 남지 않는다.
//
// 이 함수는 공개 URL이다
//   막지 않으면 누구나 남의 juso 할당량을 쓸 수 있다. `PROXY_TOKEN` 을 맞춰
//   앱만 부르게 한다. 토큰을 안 걸면 **기동 자체를 거부한다** — 열어 둔 채
//   도는 것이 가장 나쁘다.

const UPSTREAM = "https://business.juso.go.kr/addrlink";

// 중계할 엔드포인트를 못 박는다. 임의 경로를 붙일 수 있으면 이 함수가
// 아무 데나 요청을 보내주는 도구가 된다(SSRF).
const ALLOWED = new Set(["addrLinkApi.do", "addrDetailApi.do"]);

// 승인키·토큰을 실수로 흘려보내지 않도록, 넘길 파라미터를 **화이트리스트**로 둔다.
const PASS = new Set([
  "keyword", "currentPage", "countPerPage", "resultType",
  "admCd", "rnMgtSn", "udrtYn", "buldMnnm", "buldSlno",
  "searchType", "dongNm",
]);

export default async function handler(req, res) {
  const token = process.env.PROXY_TOKEN;
  if (!token) {
    return res.status(500).json({ error: "PROXY_TOKEN이 없습니다. 열린 중계는 두지 않습니다." });
  }
  if (req.headers["x-proxy-token"] !== token) {
    return res.status(401).json({ error: "unauthorized" });
  }

  const key = process.env.JUSO_CONFM_KEY;
  if (!key) {
    return res.status(500).json({ error: "JUSO_CONFM_KEY가 없습니다." });
  }

  const { path } = req.query;
  if (!ALLOWED.has(path)) {
    return res.status(400).json({ error: `허용되지 않은 경로: ${path}` });
  }

  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(req.query)) {
    if (PASS.has(k) && v != null) qs.set(k, Array.isArray(v) ? v[0] : String(v));
  }
  qs.set("confmKey", key);
  qs.set("resultType", "json");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const upstream = await fetch(`${UPSTREAM}/${path}?${qs}`, {
      signal: controller.signal,
      headers: { "User-Agent": "realestate-tax-consult/0.1 (proxy)" },
    });
    const body = await upstream.text();
    // 원문을 그대로 넘긴다. 여기서 손대면 앱의 파싱과 어긋난다.
    res.setHeader("content-type", "application/json; charset=utf-8");
    res.setHeader("cache-control", "public, max-age=86400");
    return res.status(upstream.status).send(body);
  } catch (e) {
    const aborted = e && e.name === "AbortError";
    return res.status(aborted ? 504 : 502).json({
      error: aborted ? "주소정보 누리집이 응답하지 않습니다(15초)" : String(e),
    });
  } finally {
    clearTimeout(timer);
  }
}
