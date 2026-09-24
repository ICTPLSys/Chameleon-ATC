#!/usr/bin/env bash
set -euo pipefail

source_dir=${LIBLINEAR_SOURCE:-"$HOME/chameleon-benchmarks/liblinear-multicore-2.50"}
output_base=${LIBLINEAR_OUTPUT_BASE:-"$HOME/chameleon-results/liblinear"}
dataset=${LIBLINEAR_DATASET:-}
threads=${LIBLINEAR_THREADS:-4}
solver=${LIBLINEAR_SOLVER:-6}
source_revision=${LIBLINEAR_REVISION:-2.50}
skip_build=0
run_predict=1

usage() {
    cat <<'EOF'
Usage: run-liblinear.sh --dataset FILE [options]

Options:
  --dataset FILE       LIBSVM-format training data (required)
  --source DIR         Multicore Liblinear source directory
  --output-base DIR    Parent directory for timestamped results
  --threads N          Training threads (default: 4)
  --solver N           Liblinear solver (default: 6, L1 logistic regression)
  --revision REV       Source version recorded in metadata
  --skip-build         Reuse existing train/predict binaries
  --no-predict         Do not verify the model on the training input
  -h, --help           Show this help

The Chameleon paper uses KDD12 with 120M records and a 32 GiB, 4-vCPU VM.
Scaled inputs exercise the same parser/trainer but are not paper-scale results.
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

nonnegative_integer() {
    case "$2" in ''|*[!0-9]*) die "$1 must be a non-negative integer: $2" ;; esac
}

while (( $# > 0 )); do
    case "$1" in
        --dataset) (( $# >= 2 )) || die "$1 requires a value"; dataset=$2; shift 2 ;;
        --source) (( $# >= 2 )) || die "$1 requires a value"; source_dir=$2; shift 2 ;;
        --output-base) (( $# >= 2 )) || die "$1 requires a value"; output_base=$2; shift 2 ;;
        --threads) (( $# >= 2 )) || die "$1 requires a value"; threads=$2; shift 2 ;;
        --solver) (( $# >= 2 )) || die "$1 requires a value"; solver=$2; shift 2 ;;
        --revision) (( $# >= 2 )) || die "$1 requires a value"; source_revision=$2; shift 2 ;;
        --skip-build) skip_build=1; shift ;;
        --no-predict) run_predict=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n "$dataset" ]] || die "--dataset is required"
[[ -r "$dataset" ]] || die "dataset is not readable: $dataset"
[[ -d "$source_dir" && -f "$source_dir/Makefile" ]] || die "invalid source directory: $source_dir"
positive_integer threads "$threads"
nonnegative_integer solver "$solver"

if (( skip_build == 0 )); then
    make -C "$source_dir" clean
    make -C "$source_dir" -j"$threads"
fi
[[ -x "$source_dir/train" ]] || die "train binary not found: $source_dir/train"
if (( run_predict == 1 )); then
    [[ -x "$source_dir/predict" ]] || die "predict binary not found: $source_dir/predict"
fi

run_id=$(date -u +%Y%m%dT%H%M%SZ)
result_dir="$output_base/$run_id"
mkdir -p "$result_dir"
model="$result_dir/model.txt"
records=$(wc -l <"$dataset")
dataset_bytes=$(stat -c %s "$dataset")
dataset_sha256=$(sha256sum "$dataset" | awk '{print $1}')
paper_scale=no
[[ "$records" == 120000000 ]] && paper_scale=unknown

train_command=("$source_dir/train" -s "$solver" -m "$threads" "$dataset" "$model")
{
    printf 'field\tvalue\n'
    printf 'timestamp_utc\t%s\n' "$run_id"
    printf 'hostname\t%s\n' "$(hostname)"
    printf 'kernel\t%s\n' "$(uname -r)"
    printf 'source_revision\t%s\n' "$source_revision"
    printf 'dataset\t%s\n' "$dataset"
    printf 'dataset_bytes\t%s\n' "$dataset_bytes"
    printf 'dataset_sha256\t%s\n' "$dataset_sha256"
    printf 'records\t%s\n' "$records"
    printf 'threads\t%s\n' "$threads"
    printf 'solver\t%s\n' "$solver"
    printf 'paper_scale\t%s\n' "$paper_scale"
    printf 'train_command\t'; printf '%q ' "${train_command[@]}"; printf '\n'
} >"$result_dir/metadata.tsv"
cp /proc/meminfo "$result_dir/meminfo-before.txt"

printf 'Liblinear result directory: %s\n' "$result_dir"
printf 'Training records: %s; threads: %s; solver: %s\n' "$records" "$threads" "$solver"
set +e
OMP_NUM_THREADS="$threads" OMP_PROC_BIND=close OMP_PLACES=cores \
    /usr/bin/time -v -o "$result_dir/train-time.txt" \
    stdbuf -oL -eL "${train_command[@]}" 2>&1 | tee "$result_dir/train.log"
status=${PIPESTATUS[0]}
set -e
printf '%s\n' "$status" >"$result_dir/exit-status"
printf 'train_exit_status\t%s\n' "$status" >>"$result_dir/metadata.tsv"
(( status == 0 )) || exit "$status"
[[ -s "$model" ]] || die "training succeeded but model is empty"
sha256sum "$model" >"$result_dir/model.sha256"

if (( run_predict == 1 )); then
    predict_command=("$source_dir/predict" "$dataset" "$model" "$result_dir/predictions.txt")
    set +e
    /usr/bin/time -v -o "$result_dir/predict-time.txt" \
        stdbuf -oL -eL "${predict_command[@]}" 2>&1 | tee "$result_dir/predict.log"
    predict_status=${PIPESTATUS[0]}
    set -e
    printf 'predict_exit_status\t%s\n' "$predict_status" >>"$result_dir/metadata.tsv"
    (( predict_status == 0 )) || exit "$predict_status"
fi

cp /proc/meminfo "$result_dir/meminfo-after.txt"
printf 'Liblinear completed successfully; results: %s\n' "$result_dir"
