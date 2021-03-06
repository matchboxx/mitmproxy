import re, cStringIO, traceback, json
import urwid
from PIL import Image
from PIL.ExifTags import TAGS
import lxml.html, lxml.etree
import netlib.utils
import common
from .. import utils, encoding, flow
from ..contrib import jsbeautifier, html2text

try:
    import pyamf
    from pyamf import remoting
except ImportError: # pragma nocover
    pyamf = None


VIEW_CUTOFF = 1024*50


def _view_text(content, total, limit):
    """
        Generates a body for a chunk of text.
    """
    txt = []
    for i in netlib.utils.cleanBin(content).splitlines():
        txt.append(
            urwid.Text(("text", i), wrap="any")
        )
    trailer(total, txt, limit)
    return txt


def trailer(clen, txt, limit):
    rem = clen - limit
    if rem > 0:
        txt.append(urwid.Text(""))
        txt.append(
            urwid.Text(
                [
                    ("highlight", "... %s of data not shown. Press "%utils.pretty_size(rem)),
                    ("key", "f"),
                    ("highlight", " to load all data.")
                ]
            )
        )


class ViewAuto:
    name = "Auto"
    prompt = ("auto", "a")
    content_types = []
    def __call__(self, hdrs, content, limit):
        ctype = hdrs.get_first("content-type")
        if ctype:
            ct = utils.parse_content_type(ctype) if ctype else None
            ct = "%s/%s"%(ct[0], ct[1])
            if ct in content_types_map:
                return content_types_map[ct][0](hdrs, content, limit)
            elif utils.isXML(content):
                return get("XML")(hdrs, content, limit)
        return get("Raw")(hdrs, content, limit)


class ViewRaw:
    name = "Raw"
    prompt = ("raw", "r")
    content_types = []
    def __call__(self, hdrs, content, limit):
        txt = _view_text(content[:limit], len(content), limit)
        return "Raw", txt


class ViewHex:
    name = "Hex"
    prompt = ("hex", "e")
    content_types = []
    def __call__(self, hdrs, content, limit):
        txt = []
        for offset, hexa, s in netlib.utils.hexdump(content[:limit]):
            txt.append(urwid.Text([
                ("offset", offset),
                " ",
                ("text", hexa),
                "   ",
                ("text", s),
            ]))
        trailer(len(content), txt, limit)
        return "Hex", txt


class ViewXML:
    name = "XML"
    prompt = ("xml", "x")
    content_types = ["text/xml"]
    def __call__(self, hdrs, content, limit):
        parser = lxml.etree.XMLParser(remove_blank_text=True, resolve_entities=False, strip_cdata=False, recover=False)
        try:
            document = lxml.etree.fromstring(content, parser)
        except lxml.etree.XMLSyntaxError:
            return None
        docinfo = document.getroottree().docinfo

        prev = []
        p = document.getroottree().getroot().getprevious()
        while p is not None:
            prev.insert(
                0,
                lxml.etree.tostring(p)
            )
            p = p.getprevious()
        doctype=docinfo.doctype
        if prev:
            doctype += "\n".join(prev).strip()
        doctype = doctype.strip()

        s = lxml.etree.tostring(
                document,
                pretty_print=True,
                xml_declaration=True,
                doctype=doctype or None,
                encoding = docinfo.encoding
            )

        txt = []
        for i in s[:limit].strip().split("\n"):
            txt.append(
                urwid.Text(("text", i)),
            )
        trailer(len(content), txt, limit)
        return "XML-like data", txt


class ViewJSON:
    name = "JSON"
    prompt = ("json", "j")
    content_types = ["application/json"]
    def __call__(self, hdrs, content, limit):
        lines = utils.pretty_json(content)
        if lines:
            txt = []
            sofar = 0
            for i in lines:
                sofar += len(i)
                txt.append(
                    urwid.Text(("text", i)),
                )
                if sofar > limit:
                    break
            trailer(sum(len(i) for i in lines), txt, limit)
            return "JSON", txt


class ViewHTML:
    name = "HTML"
    prompt = ("html", "h")
    content_types = ["text/html"]
    def __call__(self, hdrs, content, limit):
        if utils.isXML(content):
            parser = lxml.etree.HTMLParser(strip_cdata=True, remove_blank_text=True)
            d = lxml.html.fromstring(content, parser=parser)
            docinfo = d.getroottree().docinfo
            s = lxml.etree.tostring(d, pretty_print=True, doctype=docinfo.doctype)
            return "HTML", _view_text(s[:limit], len(s), limit)


class ViewHTMLOutline:
    name = "HTML Outline"
    prompt = ("html outline", "o")
    content_types = ["text/html"]
    def __call__(self, hdrs, content, limit):
        content = content.decode("utf-8")
        h = html2text.HTML2Text(baseurl="")
        h.ignore_images = True
        h.body_width = 0
        content = h.handle(content)
        txt = _view_text(content[:limit], len(content), limit)
        return "HTML Outline", txt


class ViewURLEncoded:
    name = "URL-encoded"
    prompt = ("urlencoded", "u")
    content_types = ["application/x-www-form-urlencoded"]
    def __call__(self, hdrs, content, limit):
        lines = utils.urldecode(content)
        if lines:
            body = common.format_keyvals(
                        [(k+":", v) for (k, v) in lines],
                        key = "header",
                        val = "text"
                   )
            return "URLEncoded form", body


class ViewMultipart:
    name = "Multipart Form"
    prompt = ("multipart", "m")
    content_types = ["multipart/form-data"]
    def __call__(self, hdrs, content, limit):
        v = hdrs.get_first("content-type")
        if v:
            v = utils.parse_content_type(v)
            if not v:
                return
            boundary = v[2].get("boundary")
            if not boundary:
                return

            rx = re.compile(r'\bname="([^"]+)"')
            keys = []
            vals = []

            for i in content.split("--" + boundary):
                parts = i.splitlines()
                if len(parts) > 1 and parts[0][0:2] != "--":
                    match = rx.search(parts[1])
                    if match:
                        keys.append(match.group(1) + ":")
                        vals.append(netlib.utils.cleanBin(
                            "\n".join(parts[3+parts[2:].index(""):])
                        ))
            r = [
                urwid.Text(("highlight", "Form data:\n")),
            ]
            r.extend(common.format_keyvals(
                zip(keys, vals),
                key = "header",
                val = "text"
            ))
            return "Multipart form", r


class ViewAMF:
    name = "AMF"
    prompt = ("amf", "f")
    content_types = ["application/x-amf"]
    def __call__(self, hdrs, content, limit):
        envelope = remoting.decode(content)
        if not envelope:
            return None

        data = {}
        data['amfVersion'] = envelope.amfVersion
        for target, message in iter(envelope):
            one_message = {}

            if hasattr(message, 'status'):
                one_message['status'] = message.status

            if hasattr(message, 'target'):
                one_message['target'] = message.target

            one_message['body'] = message.body
            data[target] = one_message
        s = json.dumps(data, indent=4)
        return "AMF", _view_text(s[:limit], len(s), limit)


class ViewJavaScript:
    name = "JavaScript"
    prompt = ("javascript", "j")
    content_types = [
        "application/x-javascript",
        "application/javascript",
        "text/javascript"
    ]
    def __call__(self, hdrs, content, limit):
        opts = jsbeautifier.default_options()
        opts.indent_size = 2
        res = jsbeautifier.beautify(content[:limit], opts)
        return "JavaScript", _view_text(res, len(content), limit)


class ViewImage:
    name = "Image"
    prompt = ("image", "i")
    content_types = [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/vnd.microsoft.icon",
        "image/x-icon",
    ]
    def __call__(self, hdrs, content, limit):
        try:
            img = Image.open(cStringIO.StringIO(content))
        except IOError:
            return None
        parts = [
            ("Format", str(img.format_description)),
            ("Size", "%s x %s px"%img.size),
            ("Mode", str(img.mode)),
        ]
        for i in sorted(img.info.keys()):
            if i != "exif":
                parts.append(
                    (str(i), str(img.info[i]))
                )
        if hasattr(img, "_getexif"):
            ex = img._getexif()
            if ex:
                for i in sorted(ex.keys()):
                    tag = TAGS.get(i, i)
                    parts.append(
                        (str(tag), str(ex[i]))
                    )
        clean = []
        for i in parts:
            clean.append([netlib.utils.cleanBin(i[0]), netlib.utils.cleanBin(i[1])])
        fmt = common.format_keyvals(
                clean,
                key = "header",
                val = "text"
            )
        return "%s image"%img.format, fmt


views = [
    ViewAuto(),
    ViewRaw(),
    ViewHex(),
    ViewJSON(),
    ViewXML(),
    ViewHTML(),
    ViewHTMLOutline(),
    ViewJavaScript(),
    ViewURLEncoded(),
    ViewMultipart(),
    ViewImage(),
]
if pyamf:
    views.append(ViewAMF())

content_types_map = {}
for i in views:
    for ct in i.content_types:
        l = content_types_map.setdefault(ct, [])
        l.append(i)


view_prompts = [i.prompt for i in views]


def get_by_shortcut(c):
    for i in views:
        if i.prompt[1] == c:
            return i


def get(name):
    for i in views:
        if i.name == name:
            return i


def get_content_view(viewmode, hdrItems, content, limit, logfunc):
    """
        Returns a (msg, body) tuple.
    """
    if not content:
        return ("No content", "")
    msg = []

    hdrs = flow.ODictCaseless([list(i) for i in hdrItems])

    enc = hdrs.get_first("content-encoding")
    if enc and enc != "identity":
        decoded = encoding.decode(enc, content)
        if decoded:
            content = decoded
            msg.append("[decoded %s]"%enc)
    try:
        ret = viewmode(hdrs, content, limit)
    # Third-party viewers can fail in unexpected ways...
    except Exception, e:
        s = traceback.format_exc()
        s = "Content viewer failed: \n"  + s
        logfunc(s)
        ret = None
    if not ret:
        ret = get("Raw")(hdrs, content, limit)
        msg.append("Couldn't parse: falling back to Raw")
    else:
        msg.append(ret[0])
    return " ".join(msg), ret[1]
