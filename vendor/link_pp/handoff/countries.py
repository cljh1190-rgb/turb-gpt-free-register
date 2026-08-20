from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class BillingAddress:
    line1: str
    city: str
    postal_code: str
    state: str = ""


@dataclass(frozen=True, slots=True)
class CountryProfile:
    code: str
    name: str
    currency: str
    locale: str
    timezone: str
    processor_entity: str
    address: BillingAddress

    @property
    def language(self) -> str:
        return self.locale

    def public_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "name": self.name,
            "currency": self.currency,
            "locale": self.locale,
            "timezone": self.timezone,
            "processor_entity": self.processor_entity,
        }

    def billing_dict(self, *, name: str, email: str) -> dict:
        return {
            "name": name,
            "email": email,
            "address": {"country": self.code, **asdict(self.address)},
        }


def _profile(
    code: str,
    name: str,
    currency: str,
    locale: str,
    timezone: str,
    address: BillingAddress,
    *,
    entity: str = "openai_llc",
) -> CountryProfile:
    return CountryProfile(code, name, currency, locale, timezone, entity, address)


# The address values are neutral, syntactically valid billing fallbacks. The
# token owner name and email are used at runtime; no person records are stored.
_COUNTRIES = (
    _profile("US", "美国", "USD", "en-US", "America/New_York", BillingAddress("1 Market St", "San Francisco", "94105", "CA")),
    _profile("BR", "巴西", "USD", "pt-BR", "America/Sao_Paulo", BillingAddress("Avenida Paulista 1000", "Sao Paulo", "01310-100", "SP")),
    _profile("GB", "英国", "GBP", "en-GB", "Europe/London", BillingAddress("1 Canada Square", "London", "E14 5AB"), entity="openai_ie"),
    _profile("FR", "法国", "EUR", "fr-FR", "Europe/Paris", BillingAddress("10 Rue de la Paix", "Paris", "75002"), entity="openai_ie"),
    _profile("DE", "德国", "EUR", "de-DE", "Europe/Berlin", BillingAddress("1 Friedrichstrasse", "Berlin", "10117"), entity="openai_ie"),
    _profile("JP", "日本", "JPY", "ja-JP", "Asia/Tokyo", BillingAddress("1-1 Marunouchi", "Chiyoda-ku", "100-0005", "Tokyo")),
    _profile("CA", "加拿大", "CAD", "en-CA", "America/Toronto", BillingAddress("100 King St W", "Toronto", "M5X 1A9", "ON")),
    _profile("AU", "澳大利亚", "AUD", "en-AU", "Australia/Sydney", BillingAddress("1 Martin Place", "Sydney", "2000", "NSW")),
    _profile("NZ", "新西兰", "NZD", "en-NZ", "Pacific/Auckland", BillingAddress("1 Queen Street", "Auckland", "1010")),
    _profile("MX", "墨西哥", "MXN", "es-MX", "America/Mexico_City", BillingAddress("Paseo de la Reforma 1", "Ciudad de Mexico", "06000", "CDMX")),
    _profile("AR", "阿根廷", "USD", "es-AR", "America/Argentina/Buenos_Aires", BillingAddress("Avenida de Mayo 100", "Buenos Aires", "C1084", "CABA")),
    _profile("CL", "智利", "USD", "es-CL", "America/Santiago", BillingAddress("Avenida Libertador 100", "Santiago", "8320000", "RM")),
    _profile("CO", "哥伦比亚", "USD", "es-CO", "America/Bogota", BillingAddress("Carrera 7 100", "Bogota", "110111", "DC")),
    _profile("PE", "秘鲁", "USD", "es-PE", "America/Lima", BillingAddress("Avenida Arequipa 100", "Lima", "15046", "Lima")),
    _profile("ES", "西班牙", "EUR", "es-ES", "Europe/Madrid", BillingAddress("Calle de Alcala 1", "Madrid", "28014"), entity="openai_ie"),
    _profile("IT", "意大利", "EUR", "it-IT", "Europe/Rome", BillingAddress("Via del Corso 1", "Roma", "00186"), entity="openai_ie"),
    _profile("NL", "荷兰", "EUR", "nl-NL", "Europe/Amsterdam", BillingAddress("Dam 1", "Amsterdam", "1012 JS"), entity="openai_ie"),
    _profile("BE", "比利时", "EUR", "nl-BE", "Europe/Brussels", BillingAddress("Rue de la Loi 1", "Bruxelles", "1000"), entity="openai_ie"),
    _profile("IE", "爱尔兰", "EUR", "en-IE", "Europe/Dublin", BillingAddress("1 Grand Canal Square", "Dublin", "D02"), entity="openai_ie"),
    _profile("PT", "葡萄牙", "EUR", "pt-PT", "Europe/Lisbon", BillingAddress("Avenida da Liberdade 1", "Lisboa", "1250-139"), entity="openai_ie"),
    _profile("AT", "奥地利", "EUR", "de-AT", "Europe/Vienna", BillingAddress("Karntner Strasse 1", "Wien", "1010"), entity="openai_ie"),
    _profile("CH", "瑞士", "CHF", "de-CH", "Europe/Zurich", BillingAddress("Bahnhofstrasse 1", "Zurich", "8001", "ZH"), entity="openai_ie"),
    _profile("SE", "瑞典", "SEK", "sv-SE", "Europe/Stockholm", BillingAddress("Drottninggatan 1", "Stockholm", "111 51"), entity="openai_ie"),
    _profile("NO", "挪威", "NOK", "nb-NO", "Europe/Oslo", BillingAddress("Karl Johans gate 1", "Oslo", "0154"), entity="openai_ie"),
    _profile("DK", "丹麦", "DKK", "da-DK", "Europe/Copenhagen", BillingAddress("Kongens Nytorv 1", "Kobenhavn", "1050"), entity="openai_ie"),
    _profile("FI", "芬兰", "EUR", "fi-FI", "Europe/Helsinki", BillingAddress("Mannerheimintie 1", "Helsinki", "00100"), entity="openai_ie"),
    _profile("PL", "波兰", "PLN", "pl-PL", "Europe/Warsaw", BillingAddress("Marszalkowska 1", "Warszawa", "00-624"), entity="openai_ie"),
    _profile("CZ", "捷克", "CZK", "cs-CZ", "Europe/Prague", BillingAddress("Vaclavske namesti 1", "Praha", "110 00"), entity="openai_ie"),
    _profile("RO", "罗马尼亚", "RON", "ro-RO", "Europe/Bucharest", BillingAddress("Calea Victoriei 1", "Bucuresti", "010061"), entity="openai_ie"),
    _profile("HU", "匈牙利", "HUF", "hu-HU", "Europe/Budapest", BillingAddress("Andrassy ut 1", "Budapest", "1061"), entity="openai_ie"),
    _profile("GR", "希腊", "EUR", "el-GR", "Europe/Athens", BillingAddress("Syntagma Square 1", "Athina", "10563"), entity="openai_ie"),
    _profile("SG", "新加坡", "SGD", "en-SG", "Asia/Singapore", BillingAddress("1 Raffles Place", "Singapore", "048616")),
    _profile("MY", "马来西亚", "MYR", "ms-MY", "Asia/Kuala_Lumpur", BillingAddress("1 Jalan Ampang", "Kuala Lumpur", "50450", "Kuala Lumpur")),
    _profile("TH", "泰国", "THB", "th-TH", "Asia/Bangkok", BillingAddress("1 Ratchadamri Road", "Bangkok", "10330", "Bangkok")),
    _profile("VN", "越南", "VND", "vi-VN", "Asia/Ho_Chi_Minh", BillingAddress("1 Le Loi", "Ho Chi Minh City", "700000", "Ho Chi Minh")),
    _profile("PH", "菲律宾", "PHP", "en-PH", "Asia/Manila", BillingAddress("1 Ayala Avenue", "Makati", "1226", "Metro Manila")),
    _profile("ID", "印度尼西亚", "IDR", "id-ID", "Asia/Jakarta", BillingAddress("1 Jalan Sudirman", "Jakarta", "10220", "DKI Jakarta")),
    _profile("IN", "印度", "INR", "en-IN", "Asia/Kolkata", BillingAddress("1 Connaught Place", "New Delhi", "110001", "Delhi")),
    _profile("KR", "韩国", "KRW", "ko-KR", "Asia/Seoul", BillingAddress("1 Sejong-daero", "Seoul", "04524", "Seoul")),
    _profile("TW", "中国台湾", "USD", "zh-TW", "Asia/Taipei", BillingAddress("1 Xinyi Road", "Taipei", "100", "Taipei")),
    _profile("HK", "中国香港", "HKD", "zh-HK", "Asia/Hong_Kong", BillingAddress("1 Queen's Road Central", "Hong Kong", "000000", "Hong Kong")),
    _profile("IL", "以色列", "ILS", "he-IL", "Asia/Jerusalem", BillingAddress("1 Rothschild Boulevard", "Tel Aviv", "6688101", "Tel Aviv")),
    _profile("AE", "阿联酋", "AED", "en-AE", "Asia/Dubai", BillingAddress("1 Sheikh Zayed Road", "Dubai", "00000", "Dubai")),
    _profile("ZA", "南非", "ZAR", "en-ZA", "Africa/Johannesburg", BillingAddress("1 Sandton Drive", "Johannesburg", "2196", "Gauteng")),
)

COUNTRIES: dict[str, CountryProfile] = {item.code: item for item in _COUNTRIES}


def get_country(code: str) -> CountryProfile:
    normalized = str(code or "").strip().upper()
    try:
        return COUNTRIES[normalized]
    except KeyError as exc:
        raise ValueError(f"不支持的国家代码: {normalized or '(空)'}") from exc


def checkout_country_for_proxy(proxy_country: CountryProfile) -> CountryProfile:
    if proxy_country.code == "BR":
        return get_country("DE")
    return proxy_country


def list_countries() -> list[dict[str, str]]:
    preferred = {"US": 0, "BR": 1, "GB": 2, "DE": 3, "FR": 4, "JP": 5, "TH": 6}
    items = sorted(
        COUNTRIES.values(),
        key=lambda item: (preferred.get(item.code, 100), item.name),
    )
    result = []
    for item in items:
        checkout_country = checkout_country_for_proxy(item)
        public = item.public_dict()
        public.update(
            checkout_country=checkout_country.code,
            checkout_currency=checkout_country.currency,
        )
        result.append(public)
    return result


def install_protocol_profiles(protocol_module: object) -> None:
    profiles = getattr(protocol_module, "LOCALE_PROFILES")
    for item in COUNTRIES.values():
        profiles[item.code] = {
            "browser_locale": item.locale,
            "browser_timezone": item.timezone,
            "browser_language": item.language,
        }
