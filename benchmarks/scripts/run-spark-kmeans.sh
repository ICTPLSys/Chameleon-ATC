#!/usr/bin/env bash
set -euo pipefail

spark_home=${SPARK_HOME:-"$HOME/chameleon-benchmarks/spark-3.3.1-bin-hadoop3"}
source_file=${SPARK_KMEANS_SOURCE:-"$HOME/chameleon-benchmarks/spark-kmeans/ChameleonSparkKMeans.java"}
output_base=${SPARK_KMEANS_OUTPUT_BASE:-"$HOME/chameleon-results/spark-kmeans"}
input=${SPARK_KMEANS_INPUT:-}
threads=${SPARK_KMEANS_THREADS:-4}
clusters=${SPARK_KMEANS_CLUSTERS:-4}
iterations=${SPARK_KMEANS_ITERATIONS:-10}
seed=${SPARK_KMEANS_SEED:-42}
partitions=${SPARK_KMEANS_PARTITIONS:-8}
driver_memory_mib=${SPARK_KMEANS_DRIVER_MEMORY_MIB:-8192}
spark_version=${SPARK_KMEANS_SPARK_VERSION:-3.3.1}
app_jar=${SPARK_KMEANS_APP_JAR:-}
skip_build=0
minimum_available_mib=

usage() {
    cat <<'EOF'
Usage: run-spark-kmeans.sh --input FILE [options]

Options:
  --input FILE              Whitespace-separated dense vectors (required)
  --spark-home DIR          Spark binary distribution directory
  --source FILE             ChameleonSparkKMeans.java source
  --app-jar FILE            Use an existing application JAR
  --output-base DIR         Parent directory for timestamped results
  --threads N               Spark local worker threads (default: 4)
  --clusters N              Number of clusters (default: 4)
  --iterations N            Maximum KMeans iterations (default: 10)
  --seed N                  KMeans initialization seed (default: 42)
  --partitions N            Input partitions (default: 8)
  --driver-memory-mib N     Spark JVM heap in MiB (default: 8192)
  --minimum-available-mib N Override heap+2GiB preflight for measured peak-sized VMs
  --spark-version VERSION   Version recorded in metadata (default: 3.3.1)
  --skip-build              Require and reuse --app-jar
  -h, --help                Show this help

The paper reports 137.5M Wikipedia-France records in a 15-GiB, 8-vCPU VM.
A smaller prefix validates the same cache/train/cost execution path, but is
not a paper-scale result.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

positive_integer() {
    case "$2" in ''|*[!0-9]*) die "$1 must be a positive integer: $2" ;; esac
    (( $2 > 0 )) || die "$1 must be greater than zero"
}

while (( $# > 0 )); do
    case "$1" in
        --input) (( $# >= 2 )) || die "$1 requires a value"; input=$2; shift 2 ;;
        --spark-home) (( $# >= 2 )) || die "$1 requires a value"; spark_home=$2; shift 2 ;;
        --source) (( $# >= 2 )) || die "$1 requires a value"; source_file=$2; shift 2 ;;
        --app-jar) (( $# >= 2 )) || die "$1 requires a value"; app_jar=$2; shift 2 ;;
        --output-base) (( $# >= 2 )) || die "$1 requires a value"; output_base=$2; shift 2 ;;
        --threads) (( $# >= 2 )) || die "$1 requires a value"; threads=$2; shift 2 ;;
        --clusters) (( $# >= 2 )) || die "$1 requires a value"; clusters=$2; shift 2 ;;
        --iterations) (( $# >= 2 )) || die "$1 requires a value"; iterations=$2; shift 2 ;;
        --seed) (( $# >= 2 )) || die "$1 requires a value"; seed=$2; shift 2 ;;
        --partitions) (( $# >= 2 )) || die "$1 requires a value"; partitions=$2; shift 2 ;;
        --driver-memory-mib) (( $# >= 2 )) || die "$1 requires a value"; driver_memory_mib=$2; shift 2 ;;
        --spark-version) (( $# >= 2 )) || die "$1 requires a value"; spark_version=$2; shift 2 ;;
        --minimum-available-mib) (( $# >= 2 )) || die "$1 requires a value"; minimum_available_mib=$2; shift 2 ;;
        --skip-build) skip_build=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n "$input" ]] || die "--input is required"
[[ -r "$input" ]] || die "input is not readable: $input"
[[ -x "$spark_home/bin/spark-submit" && -d "$spark_home/jars" ]] || \
    die "invalid Spark home: $spark_home"
positive_integer threads "$threads"
positive_integer clusters "$clusters"
positive_integer iterations "$iterations"
[[ "$seed" =~ ^[0-9]{1,9}$ ]] || die "seed must be an integer in [0, 999999999]"
positive_integer partitions "$partitions"
positive_integer driver-memory-mib "$driver_memory_mib"
(( threads <= $(nproc) )) || die "threads ($threads) exceed available vCPUs ($(nproc))"

available_mib=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
if [[ -z "$minimum_available_mib" ]]; then minimum_available_mib=$((driver_memory_mib + 2048)); fi
positive_integer minimum-available-mib "$minimum_available_mib"
(( available_mib >= minimum_available_mib )) || \
    die "required ${minimum_available_mib} MiB exceeds MemAvailable (${available_mib} MiB)"

run_id=$(date -u +%Y%m%dT%H%M%SZ)
result_dir="$output_base/$run_id"
mkdir -p "$result_dir"

if (( skip_build == 1 )); then
    [[ -n "$app_jar" && -r "$app_jar" ]] || die "--skip-build requires a readable --app-jar"
else
    [[ -r "$source_file" ]] || die "Java source is not readable: $source_file"
    command -v javac >/dev/null || die "javac is required to build the application"
    command -v jar >/dev/null || die "jar is required to build the application"
    build_dir="$result_dir/build"
    classes_dir="$build_dir/classes"
    mkdir -p "$classes_dir"
    app_jar="$build_dir/chameleon-spark-kmeans.jar"
    javac --release 8 -cp "$spark_home/jars/*" -d "$classes_dir" "$source_file" \
        >"$result_dir/build.log" 2>&1
    jar --create --file "$app_jar" -C "$classes_dir" . >>"$result_dir/build.log" 2>&1
fi

[[ -r "$app_jar" ]] || die "application JAR is not readable: $app_jar"
records=$(wc -l <"$input")
input_bytes=$(stat -c %s "$input")
input_sha256=$(sha256sum "$input" | awk '{print $1}')
app_jar_sha256=$(sha256sum "$app_jar" | awk '{print $1}')
paper_scale=no
[[ "$records" == 137500000 ]] && paper_scale=records-only

command=(
    "$spark_home/bin/spark-submit"
    --master "local[$threads]"
    --driver-memory "${driver_memory_mib}m"
    --conf spark.eventLog.enabled=false
    --conf spark.ui.enabled=false
    --conf spark.ui.showConsoleProgress=false
    --conf spark.driver.host=127.0.0.1
    --conf spark.driver.bindAddress=127.0.0.1
    --conf "spark.default.parallelism=$partitions"
    --class ChameleonSparkKMeans
    "$app_jar"
    "$input" "$clusters" "$iterations" "$partitions" "$seed"
)

{
    printf 'field\tvalue\n'
    printf 'minimum_available_mib\t%s\n' "$minimum_available_mib"
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'kernel\t%s\n' "$(uname -r)"
    printf 'spark_version\t%s\n' "$spark_version"
    printf 'input\t%s\n' "$input"
    printf 'input_bytes\t%s\n' "$input_bytes"
    printf 'input_sha256\t%s\n' "$input_sha256"
    printf 'records\t%s\n' "$records"
    printf 'dimensions\t%s\n' "$(awk 'NF {print NF; exit}' "$input")"
    printf 'threads\t%s\n' "$threads"
    printf 'clusters\t%s\n' "$clusters"
    printf 'iterations\t%s\n' "$iterations"
    printf 'initialization_seed\t%s\n' "$seed"
    printf 'partitions\t%s\n' "$partitions"
    printf 'driver_memory_mib\t%s\n' "$driver_memory_mib"
    printf 'application_jar_sha256\t%s\n' "$app_jar_sha256"
    if (( skip_build == 0 )); then printf 'application_source_sha256\t%s\n' "$(sha256sum "$source_file" | awk '{print $1}')"; fi
    printf 'paper_scale\t%s\n' "$paper_scale"
    printf 'command\t'; printf '%q ' "${command[@]}"; printf '\n'
} >"$result_dir/metadata.tsv"
cp /proc/meminfo "$result_dir/meminfo-before.txt"

printf 'Spark-KMeans result directory: %s\n' "$result_dir"
printf 'Records: %s; dimensions: %s; k: %s; iterations: %s; threads: %s\n' \
    "$records" "$(awk 'NF {print NF; exit}' "$input")" "$clusters" "$iterations" "$threads"
set +e
SPARK_LOCAL_IP=127.0.0.1 \
    /usr/bin/time -v -o "$result_dir/time.txt" \
    stdbuf -oL -eL "${command[@]}" 2>&1 | tee "$result_dir/stdout.log"
status=${PIPESTATUS[0]}
set -e
printf '%s\n' "$status" >"$result_dir/exit-status"
printf 'exit_status\t%s\n' "$status" >>"$result_dir/metadata.tsv"
cp /proc/meminfo "$result_dir/meminfo-after.txt"
(( status == 0 )) || exit "$status"
grep -q '^Cluster centers:$' "$result_dir/stdout.log" || die "cluster centers missing from output"
grep -q '^Within Set Sum of Squared Errors = ' "$result_dir/stdout.log" || \
    die "KMeans cost missing from output"
printf 'Spark-KMeans completed successfully; results: %s\n' "$result_dir"
