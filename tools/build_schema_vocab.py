"""Builds crawler/seo/data/schema_vocab.json from schema.org's published vocabulary (P12).

    python tools/build_schema_vocab.py [schemaorg-current-https.jsonld]

Without a file it downloads https://schema.org/version/latest/schemaorg-current-https.jsonld. The vocabulary is
schema.org's (CC BY-SA 3.0, see THIRD_PARTY_NOTICES.md); this script only reshapes it into a compact file:
  types      {Type: [direct parent types]}          classes and data types (attic / retired terms left out)
  datatypes  [Text, URL, Number, Integer, ...]       schema.org data types
  props      {property: [[domain types], [range types]]}
  members    {EnumerationMember: Enumeration type}   e.g. InStock -> ItemAvailability
  superseded {old term: new term}
"""
import json
import os
import sys
import urllib.request

URL = "https://schema.org/version/latest/schemaorg-current-https.jsonld"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crawler", "seo", "data",
                   "schema_vocab.json")


def _ids(v):
    if v is None:
        return []
    out = []
    for x in (v if isinstance(v, list) else [v]):
        i = x.get("@id") if isinstance(x, dict) else x
        if isinstance(i, str) and i.startswith("schema:"):
            out.append(i[7:])
    return out


def _types(node):
    t = node.get("@type")
    return t if isinstance(t, list) else [t]


def build(data):
    graph = data["@graph"]
    attic = {n["@id"][7:] for n in graph if any(p.endswith("attic.schema.org") for p in _ids(n.get("schema:isPartOf"))
                                                 or []) or "attic" in json.dumps(n.get("schema:isPartOf", ""))}
    types, props, members, superseded = {}, {}, {}, {}
    datatypes = set()
    for n in graph:
        nid = n.get("@id", "")
        if not nid.startswith("schema:"):
            continue
        name = nid[7:]
        if name in attic:
            continue
        kinds = _types(n)
        for s in _ids(n.get("schema:supersededBy")):
            superseded[name] = s
        if "rdfs:Class" in kinds:
            types[name] = _ids(n.get("rdfs:subClassOf"))
            if "schema:DataType" in kinds:
                datatypes.add(name)
        if "rdf:Property" in kinds:
            props[name] = [sorted(_ids(n.get("schema:domainIncludes"))), sorted(_ids(n.get("schema:rangeIncludes")))]
        enum = [k[7:] for k in kinds if isinstance(k, str) and k.startswith("schema:")]
        if enum and "rdfs:Class" not in kinds and "rdf:Property" not in kinds:
            members[name] = enum[0]
    for dt in list(datatypes):
        for t, parents in types.items():
            if dt in parents:
                datatypes.add(t)
    datatypes.add("DataType")
    return {"source": URL, "license": "CC BY-SA 3.0 (schema.org)", "types": types, "datatypes": sorted(datatypes),
            "props": props, "members": members, "superseded": superseded}


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        with urllib.request.urlopen(URL, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    vocab = build(data)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(vocab, f, separators=(",", ":"), sort_keys=True)
    print(f"{OUT}: {len(vocab['types'])} types, {len(vocab['props'])} properties, {len(vocab['members'])} "
          f"enumeration members, {os.path.getsize(OUT):,} bytes")


if __name__ == "__main__":
    main()
