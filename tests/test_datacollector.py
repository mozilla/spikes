# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import unittest
from spikes import datacollector as dc


class DataCollectorTest(unittest.TestCase):

    def test_is_spiking(self):
        data = {'nightly': [10, 20, 15, 9, 14, 17, 50],
                'aurora': [100, 200, 150, 90, 140, 170, 150]}
        spikes = dc.is_spiking(data, 5., 3)
        self.assertEqual(spikes, {'nightly': 'yes',
                                  'aurora': 'no'})

    def test_get_outliers(self):
        data = {'sgn::foo1': [1, 5],
                'sgn::foo2': [10, 17],
                'sgn::foo3': [7, 13],
                'sgn::foo4': [12, 1141],
                'sgn::foo5': [21, 836],
                'sgn::foo6': [8, 4],
                'sgn::foo7': [13, 4],
                'sgn::foo8': [13, 19],
                'sgn::foo9': [19, 25],
                'sgn::foo10': [32, 55],
                'sgn::foo11': [3, 9],
                'sgn::foo12': [37, 53],
                'sgn::foo13': [32, 55],
                'sgn::foo14': [47, 48],
                'sgn::foo15': [105, 115],
                'sgn::foo16': [437, 523],
                'sgn::foo17': [149, 213]}
        outliers, _ = dc.get_outliers(data)
        outliers = set(outliers)
        self.assertEqual(outliers, {'sgn::foo4',
                                    'sgn::foo5'})


if __name__ == '__main__':
    unittest.main()
