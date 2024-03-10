import sys
import argparse
import logging
import pysam
from spliceai.src.scoring import annotate
from spliceai.src.utils import *
from spliceai.src.ref import Reference
from spliceai import annotations, models
from tqdm import tqdm
from pathlib import Path
from time import time
from tensorflow.keras.models import Model, load_model
from dataclasses import dataclass
import shutil


try:
    from sys.stdin import buffer as std_in
    from sys.stdout import buffer as std_out
except ImportError:
    from sys import stdin as std_in
    from sys import stdout as std_out

try:
    from importlib import resources
except ImportError:
    import importlib_resources as resources



ANNOTATIONS = {
    'grch37': resources.path(annotations, 'grch37.txt'),
    'grch38': resources.path(annotations, 'grch38.txt')
}


@dataclass
class VariantCounter:
    n_actual: int = 0
    n_skip_chr: int = 0
    n_skip_seq: int = 0
    n_skip_precomputed: int = 0
    avg_cal_time: float = 0
    start_time: float = 0
    end_time: float = 0

    @property
    def used_time(self) -> float:
        return self.end_time - self.start_time

    def cal_avg_time(self):
        if self.n_actual == 0:
            return
        self.avg_cal_time = self.used_time / self.n_actual


def load_models() -> t.List[Model]:
    models_ = []
    for i in range(1, 6):
        with resources.path(models, f'spliceai{i}.h5') as path:
            models_.append(load_model(path))
    return models_


def get_options():

    parser = argparse.ArgumentParser(description='Adopted from spliceAI v1.3.1 and spliceAI-reforged 0.1dev1')
    parser.add_argument('-i', metavar='input', nargs='?', default=std_in, dest="input",
                        help='path to the input VCF file, defaults to standard in')
    parser.add_argument('-o', metavar='output', nargs='?', default=std_out, dest="output",
                        help='path to the output VCF file, defaults to standard out')
    parser.add_argument('-r', metavar='reference', required=True, dest="reference",
                        help='path to the reference genome fasta file')
    parser.add_argument('-a', metavar='annotation', required=True, dest="annotation",
                        help='"grch37" (GENCODE V24lift37 canonical annotation file in '
                             'package), "grch38" (GENCODE V24 canonical annotation file in '
                             'package), or path to a similar custom gene annotation file')
    parser.add_argument('-d', metavar='distance', nargs='?', default=50, dest="distance",
                        type=int, choices=range(0, 5000),
                        help='maximum distance between the variant and gained/lost splice '
                             'site, defaults to 50')
    parser.add_argument('-m', metavar='mask', default=False, dest="mask",
                        action='store_true',
                        help='mask scores representing annotated acceptor/donor gain and '
                             'unannotated acceptor/donor loss, defaults to 0')
    parser.add_argument('-s', metavar='skip', dest="skip_chr", default=None,
                        help='Skip variants with the assigned chromosomes. '
                             'The input will be split into chromosome index '
                             'based on the commas in the input string.'
                             'Ignored if this argument is not used.'
                             'Example: "1", "1,2,3,M", and "M,Y"')
    parser.add_argument("-c", "--precomputed", dest="precomputed_dir", default="precomputed",
                        )
    parser.add_argument("-t", "--n_threads", dest="n_threads", default=4,
                        help='The number of preprocessing threads to use. If your '
                             'reference assembly is located on a fast-access drive '
                             '(an SSD or a RAM-disk), using up to 4 preprocessing '
                             'threads significantly cuts down preprocessing time. '
                             'Defaults to 1.')
    parser.add_argument("-b", "--n_batch", dest="n_batch", default=1024,
                        help='The number of variant records in a VCF file to process at '
                             'a time. Using larger batches tends to cut down '
                             'preprocessing overhead. It is safe to keep the default '
                             'value. Defaults to 1024')

    parser.add_argument("-p", "--n_predict_batch", dest="n_pred_batch", default=128,
                        help='The batch size to use during inference. This option is '
                             'useful if you are using a GPU and want to exploit its full '
                             'potential. It is best to use powers of 2. If you are '
                             'getting out of memory errors, you should reduce the '
                             'batch size. Defaults to 64')

    args = parser.parse_args()

    return args


def format_vcf_record(record) -> str:
    return f"{record.chrom}_{record.pos}_{record.ref}_{record.alts}"


def read_outputs(vcf_file):
    records = []
    for record in vcf_file:
        records.append(format_vcf_record(record))
    return records


def log_process_info(var_counter: VariantCounter):
    print(f"Finished the whole process in {var_counter.used_time} secs.")
    print(f"Skipped {var_counter.n_skip_precomputed} precomputed results.")
    print(f"Skipped {var_counter.n_skip_chr} variants on the skipped chromosomes.")
    print(f"Skipped {var_counter.n_skip_seq} variants dues to sequence or reference genome issues.")
    print(f"Scored {var_counter.n_actual} variants")
    var_counter.cal_avg_time()
    print(f"Elapsed time per variant (actually calculated): {var_counter.avg_cal_time}")


def spliceai(input,
             output,
             ref_assembly,
             annotations,
             distance,
             mask,
             preprocessing_threads,
             preprocessing_batch,
             prediction_batch,
             precomputed_files_dir,
             save_computed = True):
    # parse reference assembly and annotations
    try:
        with ANNOTATIONS[annotations] as anno:
            reference = Reference(ref_assembly, anno)
    except KeyError:
        anno = Path(annotations).absolute()
        annotations = anno.stem
        if not anno.exists():
            raise ValueError(
                f'annotation file {anno} does not exist'
            )
        reference = Reference(ref_assembly, anno)
    # load models
    sp_models = load_models()

    # open the input, update the header and open the output
    vcf_input = pysam.VariantFile(input)
    n_total_vcfs = sum([1 for _ in vcf_input])
    print(f"Input data contains {n_total_vcfs} variants")
    vcf_input.reset()

    header = vcf_input.header
    header.add_line(
        '##INFO=<ID=SpliceAI,Number=.,Type=String,Description="SpliceAIv1.3.1 variant '
        'annotation. These include delta scores (DS) and delta positions (DP) for '
        'acceptor gain (AG), acceptor loss (AL), donor gain (DG), and donor loss (DL). '
        'Format: ALLELE|SYMBOL|DS_AG|DS_AL|DS_DG|DS_DL|DP_AG|DP_AL|DP_DG|DP_DL">'
    )

    if (Path(__file__).parent / precomputed_files_dir).is_dir():
        precomputed_files_dir = Path(__file__).parent / precomputed_files_dir

    pc_file_dir = Path(precomputed_files_dir) / f"{'raw' if mask else 'masked'}/{annotations}"
    if Path(output).is_file():
        copied_fn = pc_file_dir / output.name
        shutil.copy2(output, copied_fn)
        pysam.tabix_index(str(copied_fn), preset="vcf", force=True)

    vcf_output = pysam.VariantFile(output, mode="w", header=header)
    var_counter = VariantCounter()

    # break input into batches
    input_batches = iterate_batches(preprocessing_batch, vcf_input)

    # load precomputed scores via vcf

    precomputed_vars = [pysam.VariantFile(fn.with_suffix("")) for fn in pc_file_dir.iterdir() if fn.suffix == ".tbi"]

    with tqdm(total=n_total_vcfs, desc="Number of variants") as pbar:
        for batch in input_batches:
            # for every input variant `annotate` returns a list of annotation
            # strings and an optional logging message
            scores = annotate(
                preprocessing_threads,
                reference,
                sp_models,
                prediction_batch,
                distance,
                mask,
                batch,
                precomputed_vars,
                var_counter
            )
            for variant, (scores_, message) in zip(batch, scores):
                if message:
                    logging.error(message)
                variant.info['SpliceAI'] = scores_
                vcf_output.write(variant)
                pbar.update(1)
            pbar.set_description(f"Processed: "
                                 f"{var_counter.n_skip_precomputed} Precomputed,"
                                 f"{var_counter.n_actual} Calculated,"
                                 f"{var_counter.n_skip_chr} Skipped (matched skip_chroms),"
                                 f"{var_counter.n_skip_seq} Skipped (sequence or reference issues)")


if __name__ == '__main__':
    args = get_options()
    spliceai(input=args.input,
             output=args.output,
             ref_assembly=args.reference,
             annotations=args.annotation,
             distance=args.distance,
             mask=args.mask,
             preprocessing_threads=args.n_threads,
             preprocessing_batch=args.n_batch,
             prediction_batch=args.n_pred_batch,
             precomputed_files_dir=args.precomputed_dir,
             save_computed=True)
