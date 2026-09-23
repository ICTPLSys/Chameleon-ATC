# Vendored STREAM source

`stream.c` is copied without modification from
[`luhsra/hyperalloc-stream`](https://github.com/luhsra/hyperalloc-stream),
commit `54ab9af3610da6011ce9c9f597fed8dd96367165`, path `stream.c`.
`STREAM-LICENSE.txt` is the same commit's `LICENSE.txt`, also unchanged.
The complete copyright and license notice is additionally retained at the
beginning of `stream.c`.

The repository includes this copy so `../stream-check.c` builds without a
separate checkout of the original HyperAlloc workload repository. It compiles
all four kernels and adds independent checks of every array element.

The bundled license permits use, modification, and redistribution. Published
results from modified code or runs outside the STREAM Run Rules must be clearly
labelled as derived from a variant of STREAM. This project's small guest run is
a correctness workload based on a variant of STREAM; its timings are not
claimed as conforming STREAM benchmark results or paper performance results.
