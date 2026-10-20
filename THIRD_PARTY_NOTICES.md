# Third-party notices

## Meta EnCodec

The generated 24 kHz and 48 kHz combined model files contain parameters from
[Meta's official EnCodec project](https://github.com/facebookresearch/encodec),
distributed under its MIT license. Model weights are not committed here.

## encodec.cpp

Normal builds fetch the latest `main` revision of the portable native
encoder/decoder from
[HenryDelMal/encodec.cpp](https://github.com/HenryDelMal/encodec.cpp). That work
is derived from
[pfeatherstone/encodec.cpp](https://github.com/pfeatherstone/encodec.cpp) and is
distributed under the MIT License. The previous local snapshot remains as an
explicit offline fallback, with its complete notice at
`native/encodec/LICENSE`. The fetched checkout also contains its current
upstream license.

## Eigen

The native runtime vendors Eigen headers. Eigen is primarily distributed under
the Mozilla Public License 2.0. The included license is at
`native/third_party/eigen/COPYING.MPL2`.
