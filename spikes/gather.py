# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict
import re


ADDRESS = '0x[0-9a-fA-F]+'
ADDRESS_PAT = re.compile(ADDRESS)


def merge_counts(c1, c2):
    # c1 and c2 are dicts: date => count
    for d, c in c2.items():
        if d in c1:
            c1[d] += c
        else:
            c1[d] = c


def gather_modulo_addr(sgns):
    res = defaultdict(lambda: list())
    for signature in sgns:
        s = ADDRESS_PAT.split(signature)
        if len(s) == 1:
            continue

        # here we add "" around the different parts to make supersearch regex
        s = map(lambda x: '\"{}\"'.format(x), s)
        s = ADDRESS.join(s)
        res[s].append(signature)

    torm = []
    for s, signatures in res.items():
        if len(signatures) == 1:
            torm.append(s)
    for x in torm:
        del res[x]

    return res


def gather(data):
    # data is a dict: signature => { date => count }
    sgns = data.keys()
    modulo = gather_modulo_addr(sgns)
    for s, signatures in modulo.items():
        counts = data[signatures[0]]
        del data[signatures[0]]
        for sgn in signatures[1:]:
            merge_counts(counts, data[sgn])
            del data[sgn]
        data[s] = counts
