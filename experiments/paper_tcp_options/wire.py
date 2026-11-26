"""Decode physical PPP/IPv4/TCP records from the sequential reference."""
import argparse
from collections import Counter
import ipaddress
import json
from pathlib import Path
import struct


def decode(line):
    ps, node, dev, length, hex_prefix = line.split()
    wire = bytes.fromhex(hex_prefix)
    if wire[:2] != b"\x00\x21" or len(wire) < 22 or wire[11] != 6:
        return None
    iph = (wire[2] & 15) * 4
    length_ip, identification, frag = struct.unpack_from("!HHH", wire, 4)
    row = dict(ps=int(ps), node=int(node), dev=int(dev), frame=int(length), ip_bytes=length_ip,
               src=str(ipaddress.IPv4Address(wire[14:18])), dst=str(ipaddress.IPv4Address(wire[18:22])),
               id=identification, offset=(frag & 8191) * 8, more=bool(frag & 8192))
    if row["offset"]:
        return row
    tcp = wire[2+iph:]
    if len(tcp) < 20:
        raise ValueError(f"short TCP header {line!r}")
    sport, dport, seq, ack = struct.unpack_from("!HHII", tcp)
    header = (tcp[12] >> 4) * 4
    raw = tcp[20:header]
    if len(raw) != header-20:
        raise ValueError(f"truncated TCP options {line!r}")
    options = []
    offset = 0
    while offset < len(raw):
        kind = raw[offset]
        if kind == 0:
            break
        if kind == 1:
            options.append({"kind": kind})
            offset += 1
            continue
        if offset+1 >= len(raw):
            raise ValueError(f"incomplete option {line!r}")
        size = raw[offset+1]
        if size < 2 or offset+size > len(raw):
            raise ValueError(f"invalid option length {line!r}")
        payload = raw[offset+2:offset+size]
        value = {"kind": kind, "length": size}
        if kind == 8:
            value["timestamp"], value["echo"] = struct.unpack("!II", payload)
        elif kind == 5:
            value["blocks"] = list(struct.iter_unpack("!II", payload))
        elif kind == 2:
            value["mss"], = struct.unpack("!H", payload)
        options.append(value)
        offset += size
    row.update(sport=sport, dport=dport, seq=seq, ack=ack, flags=tcp[13], tcp_header=header,
               payload_here=length_ip-iph-header, options=options, option_bytes=raw.hex(), pad=raw[offset:].hex())
    return row


def summarize(path):
    shapes = Counter()
    first = {}
    multi = None
    with path.open() as source:
        for line in source:
            row = decode(line)
            if row is None:
                continue
            if row["offset"]:
                key = f"fragment+{row['offset']}/{row['frame']}"
            else:
                kinds = ",".join(str(opt["kind"]) for opt in row["options"])
                key = f"flags={row['flags']:02x} bytes={row['frame']} payload={row['payload_here']} options={kinds}"
                if multi is None and any(len(opt.get("blocks", ())) > 1 for opt in row["options"]):
                    multi = row
            shapes[key] += 1
            first.setdefault(key, row)
    return dict(shapes=dict(shapes), first=first, first_multi_sack=multi)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("wire", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps({str(path): summarize(path) for path in args.wire}, indent=2))
