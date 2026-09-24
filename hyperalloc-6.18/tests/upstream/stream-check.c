/* Run the unmodified HyperAlloc STREAM workload and give every array element
 * an independent, exit-status-enforced correctness check. All four workload
 * kernels must be enabled, matching the original validation arithmetic. */
#if !defined(COPY) || !defined(SCALE) || !defined(ADD) || !defined(TRIAD)
#error "STREAM correctness workload requires all four kernels"
#endif
#define main original_stream_main
#include "vendor/stream.c"
#undef main

int main(void)
{
    int result = original_stream_main();
    if (result)
        return result;
    STREAM_TYPE expected_a = 2.0, expected_b = 2.0, expected_c = 0.0;
    for (int iteration = 0; iteration < NTIMES; iteration++) {
        expected_c = expected_a;
        expected_b = 3.0 * expected_c;
        expected_c = expected_a + expected_b;
        expected_a = expected_b + 3.0 * expected_c;
    }
    for (size_t i = 0; i < STREAM_ARRAY_SIZE; i++) {
        if (!isfinite(a[i]) || !isfinite(b[i]) || !isfinite(c[i]) ||
            fabs((a[i] - expected_a) / expected_a) > 1e-13 ||
            fabs((b[i] - expected_b) / expected_b) > 1e-13 ||
            fabs((c[i] - expected_c) / expected_c) > 1e-13) {
            fprintf(stderr, "FAIL STREAM array data at element %zu\n", i);
            return 1;
        }
    }
    printf("PASS STREAM all-elements arrays=%d iterations=%d kernels=4\n",
           STREAM_ARRAY_SIZE, NTIMES);
    return 0;
}
