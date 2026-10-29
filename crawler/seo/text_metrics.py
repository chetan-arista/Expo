"""
Google SERP pixel widths, computed the way Screaming Frog reports "Title 1 Pixel Width" and
"Meta Description 1 Pixel Width": the text is drawn in Roboto Regular, titles at 20px and
descriptions at 14px, and each character takes its hinted (whole-pixel) advance width. Runs of
whitespace count as one space and leading/trailing whitespace is ignored.

Checked against the Screaming Frog 24.3 export in research/Report csv's: all 175 titles and all 163
meta descriptions of that crawl come out to the exact pixel value Screaming Frog reported.

The width tables below cover every character in Roboto (Latin, Greek, Cyrillic, punctuation,
currency, ...). Other characters are estimates: zero for invisible format characters and combining
accents, a full em for CJK and other wide characters, and a typical letter width for the rest.
Regenerate the tables with:  python -m crawler.seo.text_metrics path/to/Roboto-Regular.ttf
(needs Pillow; use the hinted Roboto-Regular.ttf from github.com/googlefonts/roboto, src/hinted/).
"""
import re
import unicodedata
from typing import Dict, Iterable, List, Tuple

TITLE_PX = 20                 # Google desktop result title
DESCRIPTION_PX = 14           # Google desktop result snippet

# Hinted Roboto Regular advance widths. Each run is (first code point, widths of the consecutive
# code points from there), one character per code point: width in px = ord(character) - 48.
_TITLE_RUNS: Tuple[Tuple[int, str], ...] = (
    (0x0020, '556<;?<3779;4658;;;;;;;;;;54:;:9B=<==;;>>5;=;A>>=><<<==B=<<585896;;:;;7;;55:5B;;'
             ';;7:7;:?:9:757>'),
    (0x00A0, '55;<>;5<8@99;6@97;776;:55799?@@9======C=;;;;5555=>>>>>>;>====<<<;;;;;;A:;;;;5555'
             '<;;;;;;;;;;;;9<9=;=;=;=:=:=:=:===<;;;;;;;;;;>;>;>;>;>;>;5555555555@:;5=:;;5;5;6;'
             '7;5>;>;>;;>;>;>;>;CB<7<7<7<:<:<:<:<7<7<7=;=;=;=;=;=;B?<9<<:<:<:5<>=;=<=><>?=;;;>'
             '<<7>=A67=:7;B?;>>;A=?;><:;97=7<><===<<:==;;;;::;5875GFDF@:IC@=;55>;=;=;=;=;=;;=;'
             '=;CA>;>;=:>;>;=<5IGE>;F=>;=;CA>;=;=;;;;;5555>;>;<7<7=;=;<:<7<:>;=>==<:=;;;>;>;>;'
             '>;<99>95BB==:;<::;:=?=;;<7><=8=:;<<;:;<<;;>;;>:7<;<;=;;;767797<BBB<<;;A@;7777766'
             ';;:888977=<;:?9:;<;;::::>;:<<8:9<::CBE>;?A=<99==8845569;836444337788889929662866'
             '44336665957597468488877777778876666669984555588900000000000000000000000000000000'
             '00000000000000000000000000000000000000000000000000000000400000000000000000000000'
             ';:<944><'),
    (0x037A, '4:::5;'),
    (0x0384, '5:=5;>5'),
    (0x038C, '>'),
    (0x038E, '=>6=<;>;<>>5==A>;>>='),
    (0x03A3, ';<<>=>=5<;;;6;;<:;;:;;6;;;::;<;;;:;>:>@6;;;@=;<;<;?@=>;=:;:<:?>'),
    (0x03F0, '=;:5>99<<=A?<===;;?;=<55;EF@=>=>==<;?;B<>>=>A>>>==<=?=?>CC?A==B=;;;8<;?:<<;<?<;<'
             ';::9?:<;@A<@;;@;;;;8;:555AA;;<9<B?=;B?<;B?A?GD::>>>;=:=:GE>;B?B?=;<0000000?<=;=;'
             ';9;8<:C@<:><=;=:@>?<D>DA?<=:<:<:<:=:B=>;>;>;?<?<5B?=;?<>;?<>;B@5=;=;CA;;>;>;B?<:'
             '<<><><>;>;>;=;=9=9=9>;;8A@<9=;=:<;@A@=;:C@DA<:>==;?<C?B?CA>;B?=;FBFB?<><?<JF?<?<'),
    (0x1AB0, '000000000000000'),
    (0x1D00, ';@A<;;<::5:;9?<<:==>B9;;;;;:<<@<:?:::<8;<;=<:>9;:99:;48:8=;;:89:9:>889=998888948'
             '=897889589=89988;94588989;9B=<8C<<87:7:;;:A77==<;<7>;6B<;7:8:::<=;;;;=6;8<<98998'
             '669865667457==99:9;8559998878989000000000000000000000000000000000000000000000000'
             '000000'),
    (0x1DFC, '0000=;<;<;<;=:=;=;=;=;=;;;;;;;;;;;;7>;>;>;>;>;>;7777=:=:=:;5;5;5;5ABABAB>;>;>;>;'
             '>;>;>;>;=;=;<7<7<7<7<:<:<:<:<:<7<7<7<7=;=;=;=;=;=:=:B?B?B?B?B?=:=:<9<:<:<:;7?9;7'
             '77=;=;=;=;=;=;=;=;=;=;=;=;=;;;;;;;;;;;;;;;;;5555>;>;>;>;>;>;>;>;>;>;>;>;=;=;><><'
             '><><><<9<9<9<9>9;;=;;;;;;;;;========;;;;;;'),
    (0x1F18, ';;;;;;'),
    (0x1F20, ';;;;;;;;>>>>>>>>6666666655555555;;;;;;'),
    (0x1F48, '>>>>>>'),
    (0x1F50, ';;;;;;;;'),
    (0x1F59, '<'),
    (0x1F5B, '<'),
    (0x1F5D, '<'),
    (0x1F5F, '<@@@@@@@@>>>>>>>>;;;;;;66;;;;@@'),
    (0x1F80, ';;;;;;;;========;;;;;;;;>>>>>>>>@@@@@@@@>>>>>>>>;;;;;'),
    (0x1FB6, ';;=====54589;;;'),
    (0x1FC6, ';;;;>>>8786666'),
    (0x1FD6, '665555'),
    (0x1FDD, '788;;;;;;;;<<<<=995'),
    (0x1FF2, '@@@'),
    (0x1FF6, '@@>>>>=55'),
    (0x2000, ':D:D753;5420000066;=@@8944447777;;7;59=4'),
    (0x202F, '5CG36:57:866<:99??9=9966C??9:<<:5?:9=?;9=<;4:;445'),
    (0x2070, '74'),
    (0x2074, '777777877558777777777787755'),
    (0x2090, '88988884=8985'),
    (0x20A0, '===;<B@@E??<;><J<?>=<=:<<:;@==>'),
    (0x20DB, '00'),
    (0x20E3, '7'),
    (0x20E8, '0'),
    (0x20F0, '0'),
    (0x2100, '??'),
    (0x2103, 'B'),
    (0x2105, '??'),
    (0x2109, '@'),
    (0x2113, ':'),
    (0x2116, 'E@'),
    (0x211E, '=<=>=='),
    (0x2125, ';='),
    (0x212A, '=='),
    (0x212E, '='),
    (0x2132, ';'),
    (0x213B, '?'),
    (0x214D, 'D'),
    (0x214F, 'CBAFBDACCDAB?AA@<5;@B=BGLB=BG;==B5:??:?CH?:?D5;;BB=C=:=?=EB'),
    (0x2191, '6'),
    (0x2193, '6'),
    (0x2202, ';'),
    (0x2206, '>'),
    (0x220F, '>'),
    (0x2211, '<;'),
    (0x221A, '<'),
    (0x221E, 'E'),
    (0x222B, '5'),
    (0x2248, ';'),
    (0x2260, ';'),
    (0x2264, '::'),
    (0x2423, '='),
    (0x25CA, ':'),
    (0x27E6, '9977;;9966'),
    (0x2B4E, '88'),
    (0x2B5A, '==<<<<'),
    (0x2C60, '<7<=<;7?<=;<:?A=?;DA:;9>=7;:58<<'),
    (0x2DE0, '00000000000000000000000000000000886667==;;;9::?>>>4;==779=888888995555<<;;;;;;94'
             '54444588;<N[644:647'),
    (0xA640, '<:<:<:66<;;;EA?<CA@>BAA?=<A>A?=;?<B?B?FB>;><GCI00009000000000077?<<:GC?=A>?><:?='
             '<:A=>;DAGC>;98'),
    (0xA69F, '0'),
    (0xA700, '66666666777777777799999689877444<<6665>;@>;;::=<8:EAFBEAB?B?B?=:=;=:=;<6<7@=?=GC'
             '=<?>?>><A>:8===:BA;;=<=<;9:9;;<<9>8D>9=9;><<9<<:;5>;97=;74733>97?<><:<?>=7=;=;<;'
             '?<=;?<>7>;@<>>'),
    (0xA7B0, '=<'),
    (0xA7F7, '?<>@;=A5H'),
    (0xA92E, ';'),
    (0xAB30, '=A8==7<986B<<9::CBBAA;<59:<;?6;=BB<:<:<<><978769'),
    (0xAB64, ';<'),
    (0xFB00, '<;;AA<A'),
    (0xFE20, '00000000000000'),
    (0xFEFF, '0'),
    (0xFFFC, 'EE'),
    (0x1F16A, '=='),
)

_DESCRIPTION_RUNS: Tuple[Tuple[int, str], ...] = (
    (0x0020, '34498:92556834468888888888337877=999988::4898<::9:98899<988464664887875883373<88'
             '8857587;777535:'),
    (0x00A0, '3388:7396;6784;6575548743567:;;7999999=9888844449::::::7:9999888888888<777773333'
             '88888888888887879898989797979799988787878787:8:8:8:8:8:84343434343<7849788383848'
             '584:8:8:88:8:8:8:8==95959587878787858585989898989898<;87887878738:98999:8:;9888:'
             '885:9<559758=:8::8<9:8:87875958:99998879987887784554@?>?;7B=;9843:89898989898798'
             '98=<:8:897:8:8984B@?:8?9:898=<:8989887874343:8:895959898878587:89:99979887:8:8:8'
             ':8876:64==9978877879;98785:895978888778877:88:75888898885455658<<<8888<<85555544'
             '8876666559987:7788887777:8798676877==?:7;<99679966344468624333225566557626442644'
             '33224444635475346356655555555554444446662444466600000000000000000000000000000000'
             '00000000000000000000000000000000000000000000000000000000300000000000000000000000'
             '878633:8'),
    (0x037A, '377748'),
    (0x0384, '47948:4'),
    (0x038C, ':'),
    (0x038E, '995998:88::499<:8::9'),
    (0x03A3, '888:9:94888858887887885888778888878:7:<5888<988787:;9:8978797;9'),
    (0x03F0, '9873:66889<:899988;898448??;9:9:9998;8=8::9:<:::9989;9::=>;<99=9888687;78888:888'
             '8777:788;<9;88;8778687333<<88878<;98=:87<;<:@>77:::89797@?:8<;<;9790000000;99898'
             '868697=;87:89897;:;8>:><:89787878797=9:8:8:8;8;84=;98:9:8;9:8=;49898=<87:7:7=;87'
             '88:8:8:8:8:898979797:886<;86979798<<;987>;><97:998;9=;=;=<:8<;98@=@=:8:8;9C?;8:9'),
    (0x1AB0, '000000000000000'),
    (0x1D00, '8;<9888773786:88799:=788888799;97;77786888987:78766773675977767767:6669666655636'
             '966566646695666676346566676<995=9965757877<55998885:74=8857677789878794768866665'
             '44664344533599667685446666556666000000000000000000000000000000000000000000000000'
             '000000'),
    (0x1DFC, '000098989898979898989898878787878785:8:8:8:8:8:8555597979783838383<<<<<<:8:8:8:8'
             ':8:8:8:898989595959587878787878585858598989898989797<;<;<;<;<;97978787878785;785'
             '559898989898989898989898989887878787878787874343:8:8:8:8:8:8:8:8:8:8:8:89898:9:9'
             ':9:9:987878787:788988888888899999999888888'),
    (0x1F18, '888888'),
    (0x1F20, '88888888::::::::5555555544444444888888'),
    (0x1F48, '::::::'),
    (0x1F50, '88888888'),
    (0x1F59, '8'),
    (0x1F5B, '8'),
    (0x1F5D, '8'),
    (0x1F5F, '8<<<<<<<<99::::::888888558888<<'),
    (0x1F80, '888888889999999988888888::::::::<<<<<<<<99::::::88888'),
    (0x1FB6, '889999933466888'),
    (0x1FC6, '8888:::5555555'),
    (0x1FD6, '554444'),
    (0x1FDD, '5558888888888889663'),
    (0x1FF2, '<<<'),
    (0x1FF6, '<<::99933'),
    (0x2000, '7>7>5428431000004489;;563333555588584793'),
    (0x202F, '3=@2473575448767;;696644=::678874;769;86988378333'),
    (0x2070, '53'),
    (0x2074, '555555655336555555555565533'),
    (0x2090, '6666666396654'),
    (0x20A0, '99988<;;?:;88:8B9::98979878;99:'),
    (0x20DB, '00'),
    (0x20E3, '5'),
    (0x20E8, '0'),
    (0x20F0, '0'),
    (0x2100, '::'),
    (0x2103, '<'),
    (0x2105, '::'),
    (0x2109, ';'),
    (0x2113, '7'),
    (0x2116, '>;'),
    (0x211E, '999:99'),
    (0x2125, '89'),
    (0x212A, '99'),
    (0x212E, '9'),
    (0x2132, '8'),
    (0x213B, ';'),
    (0x214D, '>'),
    (0x214F, '=<<@<><==><=;<<;947;=9=@D=9=@899<37::7:>A:7:>378<<9=979:9><'),
    (0x2191, '4'),
    (0x2193, '4'),
    (0x2202, '8'),
    (0x2206, ':'),
    (0x220F, ':'),
    (0x2211, '88'),
    (0x221A, '8'),
    (0x221E, '>'),
    (0x222B, '4'),
    (0x2248, '8'),
    (0x2260, '8'),
    (0x2264, '77'),
    (0x2423, '9'),
    (0x25CA, '7'),
    (0x27E6, '7755886644'),
    (0x2B4E, '55'),
    (0x2B5A, '998888'),
    (0x2C60, '8589985;89887:<9:8><786:95873688'),
    (0x2DE0, '0000000000000000000000000000000066444599888677::::389955796666666644448877888873'
             '4333346688EN4337435'),
    (0xA640, '878787458888?<;9=<;9=<<:98<9<;98:8=:=:?=:8:9@=A00007000000000055;887@>:9<::987;9'
             '87<9:8><@=:876'),
    (0xA69F, '0'),
    (0xA700, '44444444555555555566666467655333994444:8;:78779967?<?=?<=;=;=;979897989485;9:9@='
             '99::;::9<:769997<<889999767787886:5>:6968:88699783:8659753522:65;8:878::95989798'
             ':998:9:5:8;8::'),
    (0xA7B0, '98'),
    (0xA7F7, ';9:;89<4A'),
    (0xA92E, '7'),
    (0xAB30, '9<69958764=88777===<<8846788:489<<878788:8656546'),
    (0xAB64, '89'),
    (0xFB00, '988<<9<'),
    (0xFE20, '00000000000000'),
    (0xFEFF, '0'),
    (0xFFFC, '>>'),
    (0x1F16A, '99'),
)



def _decode(runs: Iterable[Tuple[int, str]]) -> Dict[str, int]:
    return {chr(start + i): ord(ch) - 48 for start, widths in runs for i, ch in enumerate(widths)}


# kind -> (table, typical width of a character Roboto lacks, em size in px)
_KINDS = {
    "title": (_decode(_TITLE_RUNS), 11, TITLE_PX),
    "description": (_decode(_DESCRIPTION_RUNS), 8, DESCRIPTION_PX),
}
_WS = re.compile(r"\s+")


def _char_width(c: str, table: Dict[str, int], default: int, em: int) -> int:
    w = table.get(c)
    if w is not None:
        return w
    if unicodedata.combining(c) or unicodedata.category(c) == "Cf":
        return 0              # accents and invisible characters (zero-width space, word joiner, ...)
    if unicodedata.east_asian_width(c) in ("W", "F"):
        return em
    return default


def pixel_width(text: str, kind: str = "title") -> int:
    """Width in pixels of `text` as Google would render it in a search result.
    kind: "title" (Roboto 20px) or "description" (Roboto 14px; "meta_description" also accepted)."""
    text = _WS.sub(" ", text or "").strip()
    if not text:
        return 0
    table, default, em = _KINDS["description" if kind in ("description", "meta_description") else "title"]
    return sum(_char_width(c, table, default, em) for c in text)


def _build_runs(font_path: str, px: int) -> List[Tuple[int, str]]:
    """Width runs for every character the font has a glyph for (private-use area left out).
    Needs Pillow. A character the font lacks draws the same box as U+FFFF, which is never mapped."""
    from PIL import ImageFont
    font = ImageFont.truetype(font_path, px, layout_engine=ImageFont.Layout.BASIC)

    def look(c: str):
        mask = font.getmask(c)
        return mask.size, bytes(mask), font.getlength(c), font.getbbox(c)

    missing = look(chr(0xFFFF))
    runs: List[Tuple[int, List[str]]] = []
    for cp in range(0x20, 0x30000):
        if 0xD800 <= cp <= 0xDFFF or 0xE000 <= cp <= 0xF8FF or look(chr(cp)) == missing:
            continue
        ch = chr(48 + int(font.getlength(chr(cp))))
        if runs and runs[-1][0] + len(runs[-1][1]) == cp:
            runs[-1][1].append(ch)
        else:
            runs.append((cp, [ch]))
    return [(start, "".join(chars)) for start, chars in runs]


def _runs_source(name: str, runs: List[Tuple[int, str]], chunk: int = 80) -> str:
    """Python source for a run table (long runs wrapped as adjacent string literals)."""
    lines = [f"{name}: Tuple[Tuple[int, str], ...] = ("]
    for start, widths in runs:
        parts = [repr(widths[i:i + chunk]) for i in range(0, len(widths), chunk)]
        lines.append(f"    (0x{start:04X}, {parts[0]}" + (")," if len(parts) == 1 else ""))
        for j, p in enumerate(parts[1:], 2):
            lines.append(f"             {p}" + (")," if j == len(parts) else ""))
    lines.append(")")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for table_name, size in (("_TITLE_RUNS", TITLE_PX), ("_DESCRIPTION_RUNS", DESCRIPTION_PX)):
        print(_runs_source(table_name, _build_runs(sys.argv[1], size)))
        print()
