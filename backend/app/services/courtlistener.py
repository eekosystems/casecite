"""
CourtListener Integration Service

Provides access to:
- Federal and state case law
- PACER court records
- Oral arguments
- Citation network for validation
- Free legal research API

API Documentation: https://www.courtlistener.com/api/rest-info/
"""

import asyncio
import html as html_lib
import logging
import re
from datetime import date
from enum import Enum

import httpx
from pydantic import BaseModel

from app.config import settings
from app.services.courtlistener_client import CourtListenerClientBase
from app.services.courtlistener_gate import cl_client

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html_str: str | None) -> str | None:
    """Strip an HTML opinion body down to plain text.

    Most CourtListener opinions carry an empty ``plain_text`` and only
    ``html_with_citations``. Passing raw HTML downstream silently breaks
    verbatim-quote verification (tags interleave every sentence, so no clean
    quote can ever be located) — strip here so every consumer gets real text.
    """
    if not html_str:
        return None
    text = _TAG_RE.sub(" ", html_str)
    text = html_lib.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


class CourtType(str, Enum):
    FEDERAL_APPELLATE = "federal_appellate"
    FEDERAL_DISTRICT = "federal_district"
    FEDERAL_BANKRUPTCY = "federal_bankruptcy"
    STATE_SUPREME = "state_supreme"
    STATE_APPELLATE = "state_appellate"
    STATE_TRIAL = "state_trial"


class CitationTreatment(str, Enum):
    """How a citing case treats the cited case"""

    POSITIVE = "positive"  # Followed, affirmed, approved
    NEGATIVE = "negative"  # Overruled, distinguished, criticized
    CAUTION = "caution"  # Questioned, limited
    NEUTRAL = "neutral"  # Cited, mentioned


class Opinion(BaseModel):
    """Court opinion/case from CourtListener"""

    id: int
    # The cluster (case) this opinion belongs to; v4 search results carry it.
    cluster_id: int | None = None
    absolute_url: str
    case_name: str
    case_name_short: str | None = None
    citation: list[str] | None = []
    court: str
    court_id: str
    date_filed: date | None = None
    docket_number: str | None = None
    judges: str | None = None
    text: str | None = None
    html: str | None = None
    snippet: str | None = None  # For search results
    score: float | None = None  # Relevance score


class CitingOpinion(BaseModel):
    """A case that cites another case"""

    id: int
    case_name: str
    citation: list[str]
    court: str | None = None
    date_filed: date | None = None
    treatment: CitationTreatment = CitationTreatment.NEUTRAL
    depth: int = 1  # How many times cited
    snippet: str | None = None  # Context of citation
    # True when the snippet is the passage around the citation itself (the
    # treatment scan read that passage); False when only the opinion's
    # opening text was available and the treatment is therefore neutral.
    context_found: bool = False
    url: str | None = None


class CitationValidation(BaseModel):
    """Result of scanning citing opinions for treatment language.

    This is a citing-language scan over CourtListener search results, not an
    editorial citator. ``is_good_law`` is therefore tri-state: ``True`` only
    when passages around the citation were actually read and none carried
    negative or cautionary language; ``None`` (undetermined) whenever any
    such language was found, nothing could be read, or the case was not
    found. It is never ``False`` — a regex cannot establish that a case is
    bad law, only that a court used language worth reading in full.
    """

    case_id: int
    case_name: str
    citation: str
    absolute_url: str | None = None  # Link to the case on CourtListener
    is_good_law: bool | None
    warning_level: str  # "none", "caution", "warning", "danger", "unknown"
    method: str = "citing_language_scan"
    negative_citations: list[CitingOpinion] = []
    caution_citations: list[CitingOpinion] = []
    positive_citations: list[CitingOpinion] = []
    neutral_count: int = 0
    total_citing_cases: int = 0
    citing_cases_analyzed: int = 0
    last_cited: date | None = None
    notes: list[str] = []
    analysis_basis: str = ""


class CitingLookupError(RuntimeError):
    """The opinions citing a case could not be looked up reliably."""


# Words that carry no identity in a case name ("Roe v. Wade" → {roe, wade}).
_NAME_STOPWORDS = frozenset(
    {
        "v", "vs", "the", "of", "and", "in", "re", "ex", "parte", "rel", "et", "al",
        "matter", "inc", "llc", "llp", "corp", "co", "ltd",
    }
)  # fmt: skip


def _name_tokens(name: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 1 and t not in _NAME_STOPWORDS
    }


def _normalize_citation(cite: str) -> str:
    return re.sub(r"[\s.]", "", cite).lower()


def _last_cited(citing_opinions: list[CitingOpinion]) -> date | None:
    """The most recent filing date among the citing opinions, if any carry one."""
    last_cited = None
    if citing_opinions:
        dates = [c.date_filed for c in citing_opinions if c.date_filed]
        if dates:
            last_cited = max(dates)
    return last_cited


class CourtListenerService(CourtListenerClientBase):
    """
    Integration with CourtListener API for legal research.

    Free API with rate limits. For production:
    - Register for API token at courtlistener.com
    - Consider RECAP archive for bulk data
    """

    def __init__(self):
        self.set_token(getattr(settings, "courtlistener_api_token", None))

    async def search_opinions(
        self,
        query: str,
        court_type: CourtType | None = None,
        court_ids: list[str] | None = None,
        date_after: date | None = None,
        date_before: date | None = None,
        cited_gt: int | None = None,  # Minimum citation count
        limit: int = 20,
        order_by: str = "score desc",
    ) -> list[Opinion]:
        """
        Search CourtListener for case law.

        Args:
            query: Search terms (supports boolean operators)
            court_type: Filter by court type
            court_ids: Specific court IDs (e.g., "scotus", "ca9", "nysd")
            date_after: Cases filed after this date
            date_before: Cases filed before this date
            cited_gt: Only cases cited more than N times
            limit: Max results
            order_by: Sort order (score, dateFiled)

        Returns:
            List of matching opinions
        """
        params = {
            "q": query,
            "type": "o",  # opinions
            "order_by": order_by,
            "page_size": min(limit, 100),
        }

        if court_ids:
            params["court"] = ",".join(court_ids)

        if date_after:
            params["filed_after"] = date_after.isoformat()

        if date_before:
            params["filed_before"] = date_before.isoformat()

        if cited_gt:
            params["cited_gt"] = cited_gt

        # Concurrent research (strategy briefs fan out one search per point)
        # can trip CourtListener's burst throttling — retry once with backoff
        # on 429/5xx/network errors instead of failing the whole point.
        data = None
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                async with cl_client(timeout=180.0) as client:
                    response = await client.get(
                        f"{self.BASE_URL}/search/", params=params, headers=self.headers
                    )
                    response.raise_for_status()
                    data = response.json()
                break
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    if attempt == 0:
                        await asyncio.sleep(2.0)
                        continue
                raise
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_err = e
                if attempt == 0:
                    await asyncio.sleep(2.0)
                    continue
                raise
        if data is None:
            raise last_err or RuntimeError("CourtListener search failed")

        opinions = []
        for result in data.get("results", []):
            # v4 search results are cluster-shaped: the opinion id, snippet and
            # relevance score live in the nested "opinions" list / "meta"
            # object, not at the top level. A top-level "id" does not exist,
            # so without this the id is 0 and every get_opinion() call 404s.
            nested = result.get("opinions") or []
            first = nested[0] if nested and isinstance(nested[0], dict) else {}
            meta_score = (result.get("meta") or {}).get("score") or {}
            opinion_id = first.get("id") or result.get("id") or 0
            if not opinion_id:
                logger.warning(
                    f"CourtListener search result without an opinion id skipped: "
                    f"{result.get('caseName', 'Unknown')}"
                )
                continue
            opinions.append(
                Opinion(
                    id=opinion_id,
                    cluster_id=result.get("cluster_id") or None,
                    absolute_url=f"https://www.courtlistener.com{result.get('absolute_url', '')}",
                    case_name=result.get("caseName", "Unknown"),
                    case_name_short=result.get("caseNameShort"),
                    citation=result.get("citation", []),
                    court=result.get("court", ""),
                    court_id=result.get("court_id", ""),
                    date_filed=self._parse_date(result.get("dateFiled")),
                    docket_number=result.get("docketNumber"),
                    judges=result.get("judge"),
                    snippet=first.get("snippet") or result.get("snippet"),
                    score=meta_score.get("bm25") or result.get("score"),
                )
            )

        return opinions

    async def get_opinion(self, opinion_id: int) -> Opinion | None:
        """Get full opinion text by ID."""
        async with cl_client(timeout=180.0) as client:
            response = await client.get(
                f"{self.BASE_URL}/opinions/{opinion_id}/", headers=self.headers
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()

        # Opinions carry their body in ONE of several format fields depending
        # on how CourtListener ingested them; plain_text and
        # html_with_citations are frequently empty while html_lawbox,
        # html_columbia, or xml_harvard hold the actual text. Try them all —
        # an opinion with text in a later field is not "unreadable".
        text = data.get("plain_text") or None
        if not text:
            for field in (
                "html_with_citations",
                "html",
                "html_lawbox",
                "html_columbia",
                "xml_harvard",
            ):
                text = _html_to_text(data.get(field))
                if text:
                    break
        if not text:
            logger.warning(
                f"CourtListener opinion {data.get('id')} has no usable text in any format field"
            )

        # The opinion record points at its cluster by URL (".../clusters/123/").
        cluster_match = re.search(r"/clusters/(\d+)/?$", str(data.get("cluster") or ""))
        return Opinion(
            id=data.get("id"),
            cluster_id=int(cluster_match.group(1)) if cluster_match else None,
            absolute_url=f"https://www.courtlistener.com{data.get('absolute_url', '')}",
            case_name=data.get("case_name", "Unknown"),
            case_name_short=data.get("case_name_short"),
            citation=[],  # Need to fetch from cluster
            court=data.get("court", ""),
            court_id="",
            date_filed=self._parse_date(data.get("date_filed")),
            text=text,
            html=data.get("html_with_citations"),
        )

    async def get_citing_opinions(
        self,
        cluster_id: int,
        limit: int = 100,
        citation: str | None = None,
        case_name: str | None = None,
        order_by: str = "dateFiled desc",
        opinion_ids: list[int] | None = None,
    ) -> list[CitingOpinion]:
        """
        Cases that cite a given opinion cluster, with the passage around the
        citation where CourtListener can highlight it.

        Two searches: ``cites:(ids)`` lists citing opinions in ``order_by``
        order (most recent by default; ``"citeCount desc"`` surfaces the
        most-cited citing opinions, which is where an overruling by a higher
        court tends to sit); the same query ANDed with the citation string
        (highlight=on) returns a snippet centred on the cite — that passage,
        not the opinion's opening lines, is what the treatment scan reads.

        ``cites:`` keys on OPINION ids. Pass ``opinion_ids`` when the caller
        already has them (search results carry them); otherwise they are read
        from the cluster. If neither yields an id this raises
        ``CitingLookupError`` — a cluster id is a different id space, and
        searching with it would scan whichever unrelated opinion shares the
        number and report on the wrong case.
        """
        opinion_ids = [int(i) for i in (opinion_ids or []) if i]
        async with cl_client(timeout=180.0) as client:
            if not opinion_ids:
                try:
                    cluster_resp = await client.get(
                        f"{self.BASE_URL}/clusters/{cluster_id}/", headers=self.headers
                    )
                    if cluster_resp.status_code == 200:
                        for sub in cluster_resp.json().get("sub_opinions") or []:
                            m = re.search(r"/(\d+)/?$", str(sub))
                            if m:
                                opinion_ids.append(int(m.group(1)))
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    logger.warning(f"cluster lookup failed for {cluster_id}: {e}")
            if not opinion_ids:
                raise CitingLookupError(
                    f"could not resolve the opinions in CourtListener cluster {cluster_id}"
                )
            cites_expr = "cites:(" + " OR ".join(str(i) for i in opinion_ids) + ")"

            results: list[dict] = []
            try:
                listing = await client.get(
                    f"{self.BASE_URL}/search/",
                    params={
                        "q": cites_expr,
                        "type": "o",
                        "page_size": min(limit, 100),
                        "order_by": order_by,
                    },
                    headers=self.headers,
                )
                if listing.status_code == 200:
                    results = listing.json().get("results", [])
                else:
                    logger.warning(f"citing search for {cluster_id} got HTTP {listing.status_code}")
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.error(f"Error fetching citing opinions: {e}")

            # Passage around the citation, keyed by citing cluster id.
            context: dict[int, str] = {}
            terms = [t for t in (citation, case_name) if t and len(t.strip()) > 3]
            if results and terms:
                focus = " OR ".join(f'"{t.strip()}"' for t in terms)
                try:
                    ctx_resp = await client.get(
                        f"{self.BASE_URL}/search/",
                        params={
                            "q": f"{cites_expr} ({focus})",
                            "type": "o",
                            "page_size": min(limit, 100),
                            "order_by": order_by,
                            "highlight": "on",
                        },
                        headers=self.headers,
                    )
                    if ctx_resp.status_code == 200:
                        for r in ctx_resp.json().get("results", []):
                            cid = r.get("cluster_id") or r.get("id")
                            ops = r.get("opinions") or []
                            snip = (
                                ops[0].get("snippet") if ops and isinstance(ops[0], dict) else None
                            )
                            if cid and snip and "<mark>" in snip:
                                context[int(cid)] = snip
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    logger.warning(f"citation-context search failed for {cluster_id}: {e}")

        citing: list[CitingOpinion] = []
        for result in results:
            cid = result.get("cluster_id") or result.get("id", 0)
            raw_ctx = context.get(int(cid)) if cid else None
            if raw_ctx:
                treatment = self._analyze_treatment(self._window_around_mark(raw_ctx))
                snippet = _html_to_text(raw_ctx)
                found = True
            else:
                ops = result.get("opinions") or []
                opening = ops[0].get("snippet") if ops and isinstance(ops[0], dict) else None
                snippet = _html_to_text(opening) if opening else None
                treatment = CitationTreatment.NEUTRAL
                found = False
            rel = result.get("absolute_url") or ""
            citing.append(
                CitingOpinion(
                    id=int(cid or 0),
                    case_name=result.get("caseName", "Unknown"),
                    citation=result.get("citation") or [],
                    court=result.get("court"),
                    date_filed=self._parse_date(result.get("dateFiled")),
                    treatment=treatment,
                    depth=1,
                    snippet=(snippet or "")[:600] or None,
                    context_found=found,
                    url=f"https://www.courtlistener.com{rel}" if rel else None,
                )
            )
        return citing

    @staticmethod
    def _window_around_mark(snippet: str, radius: int = 260) -> str:
        """The text near the highlighted citation — where treatment words live."""
        i = snippet.find("<mark>")
        if i < 0:
            return snippet
        return snippet[max(0, i - radius) : i + radius]

    # Word-boundary phrases only: "limit" must not fire on "limitations" and
    # "question" must not fire on "the question presented". Bare "rejected" /
    # "erroneous" are NOT negative signals: "rejected defendant's argument,
    # citing X" and "the erroneous instruction" are everyday opinion prose.
    # Negative treatment needs the verb aimed at the cited case's reasoning.
    _NEGATIVE_RE = re.compile(
        r"\b(overrul(?:e|ed|es|ing)|abrogat(?:e|ed|es|ing)|disapprov(?:e|ed|es|ing)|"
        r"reject(?:ed|s|ing)? (?:the )?(?:reasoning|holding|rule|analysis|approach|rationale|"
        r"conclusion) (?:of|in)|no longer good law|superseded by statute|reversed on|"
        r"wrongly decided|erroneously decided|declin(?:e|ed|es|ing) to follow|receded from|"
        r"repudiat(?:e|ed|es|ing)|"
        # Subsequent-history notation next to a cite: "Smith, 1 F.3d 1, rev'd,
        # 500 U.S. 1" / "vacated, ..." — the cited decision itself was undone.
        r"rev['’]d(?: in part)?|vacated(?: in part)?)\b",
        re.IGNORECASE,
    )
    # "Overruled on other grounds" is Word-of-art for "the point you're citing
    # it for still stands" — a caution, not a negative.
    _OTHER_GROUNDS_RE = re.compile(
        r"\b(overrul|abrogat|disapprov|reject|supersed|rev['’]d|vacat)\w*\s+"
        r"(?:in part\s+)?on other grounds\b",
        re.IGNORECASE,
    )
    _CAUTION_RE = re.compile(
        r"\b(distinguish(?:ed|able|es|ing)?|questioned|criticiz(?:ed|es|ing)|"
        r"limited to (?:its|their) facts|narrow(?:ed|ing)|cast(?:s)? doubt|"
        r"called into doubt|doubted|on other grounds)\b",
        re.IGNORECASE,
    )
    _POSITIVE_RE = re.compile(
        r"\b(followed|affirm(?:ed|s|ing)|approved|adopted|relied (?:up)?on|controlling|"
        r"reaffirm(?:ed|s|ing)|endorsed|consistent with)\b",
        re.IGNORECASE,
    )

    def _analyze_treatment(self, text: str) -> CitationTreatment:
        """Classify the language near a citation. Negative wins over caution
        wins over positive, because an opinion that says "followed until
        overruled" is telling you about the overruling — except that
        "overruled on other grounds" is demoted to caution."""
        text = _html_to_text(text) or ""
        stripped = self._OTHER_GROUNDS_RE.sub("on other grounds", text)
        if self._NEGATIVE_RE.search(stripped):
            return CitationTreatment.NEGATIVE
        if self._CAUTION_RE.search(stripped):
            return CitationTreatment.CAUTION
        if self._POSITIVE_RE.search(stripped):
            return CitationTreatment.POSITIVE
        return CitationTreatment.NEUTRAL

    _NOT_A_CITATOR = (
        "This is a scan of the language courts use near the citation in CourtListener "
        "search results — not an editorial citator. It does not replace Shepard's or "
        "KeyCite; read the flagged opinions before relying on the case."
    )

    async def validate_citation(self, citation: str) -> CitationValidation:
        """
        Scan citing opinions for treatment language around a citation.

        This reads the language citing courts use near the citation in the
        most recent AND the most-cited opinions that cite it. It is not an
        editorial citator: a clean result means no negative language was
        found in the passages that could be read, not that an editor has
        certified the case; a flagged result means a court used language
        worth reading in full, not that the case has been overruled.

        Args:
            citation: The citation to validate (e.g., "410 U.S. 113")

        Returns:
            Validation result with warning level, tri-state is_good_law, and
            the citing opinions grouped by the language found.
        """
        self._check_token()

        # First, search for the case to get the cluster_id
        cluster_id = None
        case_name = "Not Found"
        case_name_short = None
        total_citations = 0
        absolute_url = None
        found_citations: list[str] = []
        opinion_ids: list[int] = []

        first_result, nearest = await self._search_cited_case(citation)
        if first_result is not None:
            opinion_ids = [
                o["id"]
                for o in first_result.get("opinions") or []
                if isinstance(o, dict) and o.get("id")
            ]
            cluster_id = first_result.get("cluster_id") or first_result.get("id")
            case_name = first_result.get("caseName", "Unknown")
            case_name_short = first_result.get("caseNameShort") or None
            total_citations = first_result.get("citeCount", 0)
            found_citations = first_result.get("citation") or []
            rel_url = first_result.get("absolute_url", "")
            if rel_url:
                absolute_url = f"https://www.courtlistener.com{rel_url}"

        if not cluster_id:
            return self._not_found_validation(citation, nearest)

        # The citation string that citing opinions are most likely to use.
        cite_term = (
            citation
            if self._looks_like_citation(citation)
            else (found_citations[0] if found_citations else None)
        )
        name_term = case_name_short or (case_name if " v. " in case_name else None)
        try:
            citing_opinions = await self._citing_opinions_recent_and_most_cited(
                cluster_id, cite_term, name_term, opinion_ids
            )
        except CitingLookupError as e:
            logger.warning(f"citation check could not run for cluster {cluster_id}: {e}")
            return self._lookup_failed_validation(
                citation,
                cluster_id=cluster_id,
                case_name=case_name,
                absolute_url=absolute_url,
                total_citations=total_citations,
            )

        negative = [c for c in citing_opinions if c.treatment == CitationTreatment.NEGATIVE]
        caution = [c for c in citing_opinions if c.treatment == CitationTreatment.CAUTION]
        positive = [c for c in citing_opinions if c.treatment == CitationTreatment.POSITIVE]
        neutral = [c for c in citing_opinions if c.treatment == CitationTreatment.NEUTRAL]
        with_context = sum(1 for c in citing_opinions if c.context_found)

        warning_level, is_good_law = self._treatment_verdict(
            negative=len(negative), caution=len(caution), read=with_context
        )

        notes = self._treatment_notes(
            total_citations=total_citations,
            citing_opinions=citing_opinions,
            negative=negative,
            caution=caution,
            positive=positive,
            with_context=with_context,
        )

        return CitationValidation(
            case_id=cluster_id,
            case_name=case_name,
            citation=citation,
            absolute_url=absolute_url,
            is_good_law=is_good_law,
            warning_level=warning_level,
            negative_citations=negative[:25],
            caution_citations=caution[:25],
            positive_citations=positive[:25],
            neutral_count=len(neutral),
            total_citing_cases=total_citations or len(citing_opinions),
            citing_cases_analyzed=len(citing_opinions),
            last_cited=_last_cited(citing_opinions),
            notes=notes,
            analysis_basis="language_near_citation" if with_context else "opening_text_only",
        )

    async def _search_cited_case(self, citation: str) -> tuple[dict | None, str | None]:
        """Search CourtListener for the cited case; returns (matching result, nearest miss name)."""
        nearest: str | None = None
        first_result = None
        async with cl_client(timeout=180.0) as client:
            # Search for the citation
            search_query = (
                f'citation:"{citation}"' if self._looks_like_citation(citation) else citation
            )
            response = await client.get(
                f"{self.BASE_URL}/search/",
                params={"q": search_query, "type": "o", "page_size": 5},
                headers=self.headers,
            )

            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])
                # Search ranks by relevance: the first hit is not necessarily the
                # case asked about. Take the first result that actually IS it.
                first_result = next((r for r in results if self._result_matches(citation, r)), None)
                if results and first_result is None:
                    nearest = results[0].get("caseName")
            else:
                response.raise_for_status()
        return first_result, nearest

    def _not_found_validation(self, citation: str, nearest: str | None) -> CitationValidation:
        """The undetermined result for a citation CourtListener could not match."""
        not_found = (
            "Citation not found in CourtListener database. Try a different format "
            "(e.g., '410 U.S. 113' or 'Roe v. Wade')."
        )
        if nearest:
            not_found = (
                "No case in CourtListener matched this citation exactly, so nothing was "
                f'checked. The closest search result was "{nearest}" — if that is the '
                "case you meant, search by its reporter citation (e.g., '410 U.S. 113')."
            )
        return CitationValidation(
            case_id=0,
            case_name="Not Found",
            citation=citation,
            is_good_law=None,
            warning_level="unknown",
            notes=[not_found, self._NOT_A_CITATOR],
            analysis_basis="not_found",
        )

    def _lookup_failed_validation(
        self,
        citation: str,
        *,
        cluster_id: int,
        case_name: str,
        absolute_url: str | None,
        total_citations: int,
    ) -> CitationValidation:
        """The undetermined result when the citing opinions could not be looked up."""
        return CitationValidation(
            case_id=cluster_id,
            case_name=case_name,
            citation=citation,
            absolute_url=absolute_url,
            is_good_law=None,
            warning_level="unknown",
            total_citing_cases=total_citations,
            notes=[
                self._NOT_A_CITATOR,
                "Could not verify: the opinions citing this case could not be looked up "
                "in CourtListener, so nothing was scanned. This is not a clean result — "
                "try again, or check the case in a citator.",
            ],
            analysis_basis="citing_lookup_failed",
        )

    def _treatment_notes(
        self,
        *,
        total_citations: int,
        citing_opinions: list[CitingOpinion],
        negative: list[CitingOpinion],
        caution: list[CitingOpinion],
        positive: list[CitingOpinion],
        with_context: int,
    ) -> list[str]:
        """Human-readable notes describing what was scanned and what language was found."""
        # Build notes — the method caveat first, because it frames everything else.
        notes = [self._NOT_A_CITATOR]
        if total_citations > 0:
            notes.append(f"Cited by {total_citations:,} opinions in CourtListener.")
        if citing_opinions:
            notes.append(
                f"Scanned the passage around the citation in {with_context} of the "
                f"{len(citing_opinions)} most recent and most-cited citing opinions retrieved; "
                "the rest could not be located in the text and were not classified."
            )
        else:
            notes.append(
                "No citing opinions could be retrieved — the case may be too recent or the search was limited."
            )
        if negative:
            notes.append(
                f"{len(negative)} citing opinion(s) use negative language near the citation "
                "(overruled, abrogated, declined to follow…). Read each before relying on the case."
            )
        if caution:
            notes.append(
                f"{len(caution)} citing opinion(s) use cautionary language (distinguished, questioned, "
                "limited, on other grounds…)."
            )
        if positive:
            notes.append(
                f"{len(positive)} citing opinion(s) use positive language (followed, affirmed, relied on…)."
            )
        if with_context == 0 and citing_opinions:
            notes.append(
                "None of the retrieved opinions could be read at the citation, so no treatment "
                "signal is available — this is not a clean result."
            )
        return notes

    @staticmethod
    def _treatment_verdict(*, negative: int, caution: int, read: int) -> tuple[str, bool | None]:
        """Map treatment counts to (warning_level, is_good_law).

        ``is_good_law`` is ``True`` only for a clean read: at least one passage
        was actually read and none carried negative or cautionary language.
        Any negative hit makes it undetermined (``None``) — one "overruled" from
        a state supreme court is decisive, one "declined to follow" from a
        trial court elsewhere is not, and a regex cannot tell them apart. It
        is never ``False``.
        """
        if negative >= 3:
            return "danger", None
        if negative >= 1:
            return "warning", None
        if caution >= 3:
            return "warning", None
        if caution >= 1:
            return "caution", None
        if read == 0:
            return "unknown", None
        return "none", True

    def _result_matches(self, query: str, result: dict) -> bool:
        """True when a search result is the case the query names.

        A reporter citation must appear among the result's citations; a case
        name must have every identifying word in the result's case name.
        """
        if self._looks_like_citation(query):
            wanted = _normalize_citation(query)
            for cite in result.get("citation") or []:
                have = _normalize_citation(str(cite))
                if have and (have in wanted or wanted in have):
                    return True
            return False
        wanted_tokens = _name_tokens(query)
        have_tokens = _name_tokens(
            f"{result.get('caseName') or ''} {result.get('caseNameShort') or ''}"
        )
        return bool(wanted_tokens) and wanted_tokens <= have_tokens

    async def _citing_opinions_recent_and_most_cited(
        self,
        cluster_id: int,
        cite_term: str | None,
        name_term: str | None,
        opinion_ids: list[int] | None = None,
    ) -> list[CitingOpinion]:
        """The most recent citing opinions plus the most-cited ones, de-duplicated.

        Recency alone misses the case that matters most: an overruling by a
        higher court that happened years ago and is itself heavily cited. The
        most-cited citing opinions are the cheapest proxy for authority that
        CourtListener's search API can sort by.
        """
        recent = await self.get_citing_opinions(
            cluster_id,
            limit=100,
            citation=cite_term,
            case_name=name_term,
            opinion_ids=opinion_ids,
        )
        try:
            most_cited = await self.get_citing_opinions(
                cluster_id,
                limit=100,
                citation=cite_term,
                case_name=name_term,
                order_by="citeCount desc",
                opinion_ids=opinion_ids,
            )
        except (httpx.HTTPError, ValueError, KeyError, CitingLookupError) as e:
            logger.warning(f"most-cited citing search failed for {cluster_id}: {e}")
            most_cited = []
        # An opinion can be in both lists. Keep the copy whose passage around
        # the citation was read: the context search can fail for one listing
        # and succeed for the other, and the unread copy says "neutral" about
        # an opinion the read copy knows overruled the case.
        by_id: dict[int, CitingOpinion] = {}
        for opinion in [*most_cited, *recent]:
            have = by_id.get(opinion.id)
            if have is None or (opinion.context_found and not have.context_found):
                by_id[opinion.id] = opinion
        return list(by_id.values())

    async def search_by_topic(
        self, topic: str, jurisdiction: str | None = None, limit: int = 20
    ) -> list[Opinion]:
        """
        Search for cases by legal topic.

        Uses CourtListener's full-text search with legal-aware query expansion.
        """
        # Expand common legal topics to search terms
        topic_expansions = {
            "h1b": '"H-1B" OR "specialty occupation" OR "labor condition application"',
            "asylum": 'asylum OR "credible fear" OR "well-founded fear" OR persecution',
            "removal": 'removal OR deportation OR "cancellation of removal"',
            "naturalization": "naturalization OR citizenship OR N-400",
            "trademark": 'trademark OR "likelihood of confusion" OR "trade dress"',
            "patent": 'patent OR "claim construction" OR obviousness OR anticipation',
            "contract": 'contract OR breach OR "consideration" OR "meeting of minds"',
            "negligence": 'negligence OR "duty of care" OR "proximate cause" OR "reasonable person"',
        }

        query = topic_expansions.get(topic.lower(), topic)

        court_ids = None
        if jurisdiction:
            court_ids = self._get_courts_for_jurisdiction(jurisdiction)

        return await self.search_opinions(
            query=query,
            court_ids=court_ids,
            limit=limit,
            cited_gt=5,  # Only well-cited cases
        )

    def _looks_like_citation(self, text: str) -> bool:
        """Check if text looks like a legal citation."""
        # Matches patterns like "410 U.S. 113" or "123 F.3d 456"
        citation_pattern = r"\d+\s+[A-Za-z.]+\s*\d*[a-z]*\s+\d+"
        return bool(re.search(citation_pattern, text))


# Singleton instance
courtlistener_service = CourtListenerService()


def apply_courtlistener_token(token: str | None) -> None:
    """Propagate a CourtListener token to EVERY service that holds its own client.

    There are three CourtListener-backed services sharing one client base
    (``courtlistener_client``), each instance caching the token in its own
    ``headers``: ``courtlistener_service`` (validation,
    RAG), ``legal_tools`` (case search, precedents, dockets, oral args, trends),
    and ``judge_intel`` (judge search/profiles). They must all be updated
    together or some Legal Tools work while others raise "token required".

    Imported lazily to avoid circular imports at module load.
    """
    courtlistener_service.set_token(token)
    try:
        from app.services.legal_tools import legal_tools

        legal_tools.set_token(token)
    except Exception:  # pragma: no cover - service optional/unavailable
        logger.exception("Failed to apply CourtListener token to legal_tools service")
    try:
        from app.services.judge_intel import judge_intel

        judge_intel.set_token(token)
    except Exception:  # pragma: no cover - service optional/unavailable
        logger.exception("Failed to apply CourtListener token to judge_intel service")
