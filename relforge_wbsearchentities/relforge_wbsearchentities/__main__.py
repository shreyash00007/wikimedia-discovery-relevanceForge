from collections import defaultdict
from functools import partial
from glob import glob
from gzip import GzipFile
import hashlib
import json
from json.decoder import JSONDecodeError
import logging
import os
import pickle
import pprint
import time

import numpy as np
import pandas as pd
import requests
import tensorflow as tf
from tqdm import tqdm

from relforge.cli_utils import \
    iterate_pickle, load_pkl, with_arg, bounded_float, positive_int, \
    with_pkl_df, with_elasticsearch, with_sql_query, \
    DELETE_ON_ERROR, DELETE_ON_EXIT, generate_cli
from relforge_wbsearchentities.explain_parser import \
    explain_parser_from_root, parse_hits, merge_explains
from relforge_wbsearchentities.tf_optimizer import \
    HyperoptOptimizer, AutocompleteEvaluator, SensitivityAnalyzer, \
    tf_run_all, EXAM_PROB


log = logging.getLogger(__name__)
WIKIDATA_API_URL = 'https://www.wikidata.org/w/api.php'


def prefixes(string):
    """Return all prefixes of provided string from shortest to longest"""
    assert isinstance(string, str)
    return (string[:i] for i in range(1, len(string) + 1))


def read_tfrecord_dataset_args(args):
    """Helper to call read_tfrecord_dataset as with_arg loader"""
    return dict(args, dataset=read_tfrecord_dataset(
        args['dataset'], args['equation'], args['batch_size']))


def read_tfrecord_dataset(in_path, explain, batch_size=64 * 1024):
    """Read tfrecords generated by `make_tfrecord`"""
    def parse_record(example_proto):
        features = dict({
            k: tf.VarLenFeature(dtype=tf.float32)
            for k in explain.feature_vec().keys()
        }, **{
            'meta/page_id': tf.FixedLenFeature([1], dtype=tf.int64),
            'meta/explain_value': tf.FixedLenFeature([1], dtype=tf.float32),
            'meta/prefix': tf.FixedLenFeature([1], dtype=tf.string),
        })
        return tf.parse_single_example(example_proto, features)

    def decode_sparse(parsed_features):
        for k, v in parsed_features.items():
            if isinstance(v, (tf.SparseTensor, tf.sparse.SparseTensor)):
                parsed_features[k] = tf.sparse.to_dense(v)
        return parsed_features

    in_path = list(glob(in_path))

    # Hopefully this is standard...evaluating a simple graph
    # goes from ~50k hits/s to 5M hits/s when cache is on tmpfs
    # This needs to be unique per input files, because tensorflow
    # doesn't check if the cache matches the input it simply accepts
    # it.
    path_hash = hashlib.md5(''.join(in_path).encode('utf8')).hexdigest()
    cache_path = '/run/user/{}/relforge-tf-ac.{}'.format(
        os.getuid(), path_hash)
    DELETE_ON_EXIT.append(cache_path + '*')

    dataset = (
        tf.data.TFRecordDataset(in_path)
        .apply(tf.data.experimental.map_and_batch(
            map_func=parse_record,
            batch_size=batch_size))
        .map(decode_sparse)
        .cache(cache_path))

    return dataset


def load_source_df_args(args):
    """Helper to call read_tfrecord_dataset as with_arg loader"""
    df_source = load_source_df(
        args['df_source'],
        args.get('resample', None),
        args.get('seed', 0))
    return dict(args, df_source=df_source)


def load_source_df(in_path, resample=None, seed=0):
    df = load_pkl(in_path)
    df.columns = df.columns.str.lower()
    # As long as we use the same seed this will choose same
    # rows on separate executions.
    if resample is not None and resample > 0:
        # TODO: sample complexity probably varies by language,
        # for example the number of characters in their alphabet.
        # Check if that matters.
        g = df.groupby(['context', 'language'], group_keys=False)
        df = g.apply(lambda x: x.sample(n=min(len(x), resample)))
    df['searchterm'] = df['searchterm'].astype(str).str.lower().str.strip()
    return df


def iterate_lucene_explains(paths):
    if isinstance(paths, str):
        paths = list(glob(paths))
    with tqdm(desc='hits') as hits_pbar:
        for one_path in tqdm(paths, 'paths'):
            try:
                for row, hits in iterate_pickle(one_path):
                    yield row, hits
                    hits_pbar.update(len(hits))
            except:  # noqa: E722
                log.error('Failed while reading %s'.format(one_path))
                raise


# Various CLI args re-used throughout. All args are responsible for converting
# the argument into a directly usable form, for example converting file paths
# into their contents
with_batch_size = with_arg('-b', '--batch-size', dest='batch_size', type=positive_int, default=1000, required=False)
with_resample = with_arg('-r', '--resample', dest='resample', type=positive_int, default=None, required=False)
with_seed = with_arg('--seed', type=int, default=0, required=False)
with_es_query = with_arg('--es-query', dest='es_query', type=load_pkl, required=True)
with_equation = with_arg('-e', '--equation', dest='equation', type=load_pkl, required=True)
with_source_dataset = with_arg('-s', '--source-dataset', dest='df_source', loader=load_source_df_args, required=True)
with_lucene_explains = with_arg(
    '-l', '--lucene-explain', dest='lucene_explains', type=iterate_lucene_explains, required=True)
with_tfrecords = with_arg('-t', '--tfrecord', dest='dataset', loader=read_tfrecord_dataset_args, required=True)
with_restarts = with_arg('--restarts', dest='restarts', type=positive_int, default=5)
with_epochs = with_arg('--epochs', dest='epochs', type=positive_int, default=200)
with_test_size = with_arg('--test-size', dest='test_size', type=bounded_float(0, 1), default=0.5)
with_top_k = with_arg('--top-k', dest='top_k', type=positive_int, default=len(EXAM_PROB))
with_language = with_arg('--language', dest='language', required=True)
with_context = with_arg('--context', dest='context', required=True)
with_train_report = with_arg('--train-report', dest='train_report', type=load_pkl)


# The main handler for registering and choosing commands from cli
main = generate_cli()


@main.command(with_sql_query)
def fetch_source(sql_query, out_path):
    """Load input dataset from sql query definition"""
    df_raw = sql_query.to_df()
    # Drop groups that are too small.
    g = df_raw.groupby(['context', 'language'])
    df_filtered = df_raw[g['dt'].transform('size') > 1000]
    # This metadata helps the Makefile generate all the appropriate rules.
    # two cols with sep='_' gives us a {context}-{language} on each line
    # The columns may contain -, so we use _ to ensure no quoting happens
    df_unique = df_filtered[['context', 'language']].drop_duplicates()
    metadata_str = df_unique.to_csv(sep='_', header=False, index=False)
    DELETE_ON_ERROR.append(out_path + '.meta')
    with open(out_path + '.meta', 'w') as f:
        f.write(metadata_str)
    with GzipFile(out_path, 'w') as f:
        pickle.dump(df_filtered, f, pickle.HIGHEST_PROTOCOL)


@main.command(with_source_dataset, with_resample, with_batch_size(default=1000), with_seed)
def expand_and_split_queries(df_source, out_path, resample, batch_size, seed):
    """Converts a single input csv into many work pieces

    Expands searchterms from the source dataset into the full set of possible
    prefix queries. The set of queries is written out to individual files
    containing `batch_size` queries each.
    """
    # Expand searchterm with all it's prefixes
    # Not sure how to do this in pandas directly, flatMap doesn't seem to be a thing
    all_prefixes = defaultdict(set)
    for index, row in df_source.iterrows():
        key = (row['context'], row['language'])
        all_prefixes[key].update(prefixes(row['searchterm']))
    df_prefix = pd.DataFrame(
        (k + (p,) for k, prefixes in all_prefixes.items() for p in prefixes),
        columns=('context', 'language', 'prefix'))

    # Write results per (context,language)
    df_grouped = df_prefix.groupby(['context', 'language'])
    all_dfs = {g: df_grouped.get_group(g) for g in df_grouped.groups}
    for (context, language), df_one in all_dfs.items():
        log.info('Generating splits for (%s, %s) with %d searches to perform', context, language, len(df_one))
        for i, start in enumerate(range(1, len(df_one), batch_size)):
            batch_filename = 'query-{:04d}.{}_{}.pkl.gz'.format(i, context, language)
            batch_out_path = os.path.join(out_path, batch_filename)
            log.info('Writing query split %s', (batch_out_path))
            df_batch = df_one.iloc[start:start+batch_size]
            with GzipFile(batch_out_path, 'wb') as f:
                pickle.dump(df_batch, f, pickle.HIGHEST_PROTOCOL)


@main.command(
    with_context, with_language,
    with_arg('--api-url', dest='api_url', default=WIKIDATA_API_URL))
def fetch_wbsearchentities_query(out_path, context, language, api_url):
    """Fetch elasticsearch queries for each unique (context,language) pair"""
    session = requests.Session()
    es_query = session.get(api_url, params={
        'action': 'wbsearchentities',
        'format': 'json',
        'search': '{{query_string}}',
        'type': context,
        'language': language,
        'cirrusDumpQuery': 1,
    }).json()['query']

    with GzipFile(out_path, 'wb') as f:
        pickle.dump(es_query, f, pickle.HIGHEST_PROTOCOL)


@main.command(
    with_pkl_df, with_es_query, with_batch_size(default=50), with_elasticsearch,
    with_arg('-i', '--index', dest='index', default='wikidatawiki_content', required=False))
def fetch_explain(df, out_path, es_query, batch_size, es, index):
    """Retrieve explains for all queries"""
    encoded_query = json.dumps(es_query['query'])
    try:
        encoded_rescore = json.dumps(es_query['rescore'])
    except KeyError:
        encoded_rescore = None

    with GzipFile(out_path, 'wb') as f:
        for _, row in df.iterrows():
            # Template queries are deprecated as of 5.0.0, so lets do our
            # own replacement i guess.
            try:
                templated_query = json.loads(
                    encoded_query
                    .replace('"{{query_string}}"', json.dumps(row['prefix']))
                    .replace('"{{QUERY_STRING}}"', json.dumps(row['prefix'].upper())))
            except JSONDecodeError:
                log.warning('Invalid string, bad utf8? str:`{}` json:`{}`'.format(row['prefix']))
                continue
            local_query = {
                'size': batch_size,
                'query': templated_query,
                'explain': True,
                '_source': False,
            }
            if encoded_rescore is not None:
                local_query['rescore'] = json.loads(
                    encoded_rescore
                    .replace('"{{query_string}}"', json.dumps(row['prefix']))
                    .replace('"{{QUERY_STRING}}"', json.dumps(row['prefix'].upper())))
            res = es.search(index=index, body=local_query)
            pickle.dump((row, res['hits']['hits']), f, pickle.HIGHEST_PROTOCOL)


@main.command(with_lucene_explains, with_es_query)
def make_equation(lucene_explains, out_path, es_query):
    """Aggregate explains into an equation

    Parses explains and merges them together until we have an explain that
    represents the full scoring equation. This is necessary as individual
    explains only describe the portions of the query they matched.
    """
    parser = explain_parser_from_root(es_query)
    base_explain = None
    seen = 0
    for row, hits in lucene_explains:
        base_explain = merge_explains(parser, parse_hits(parser, hits), base_explain)
        seen += len(hits)
        if base_explain.is_complete:
            break
    if not base_explain.is_complete:
        # Although the equation is not complete, by definition it represents all explains
        # in this dataset and is therefore "good enough".
        log.warning('%s is incomplete!', os.path.basename(out_path))
    with GzipFile(out_path, 'wb') as f:
        pickle.dump(base_explain, f, pickle.HIGHEST_PROTOCOL)
    log.info('Parsed %d explains for %s', seen, os.path.basename(out_path))


def _float_feature(value):
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))


def _int64_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def _bytes_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=value))


def _unicode_feature(value):
    return _bytes_feature([x.encode('utf8') for x in value])


def extract_features(row, page_id, explain):
    feature = {
        'meta/page_id': _int64_feature([int(page_id)]),
        'meta/explain_value': _float_feature([explain.value]),
        'meta/prefix': _unicode_feature([row['prefix']]),
    }
    for k, v in explain.feature_vec().items():
        feature[k] = _float_feature(v)
    return feature


@main.command(with_lucene_explains, with_es_query, with_equation, with_context, with_language)
def make_tfrecord(lucene_explains, out_path, es_query, equation, context, language):
    # TODO: Should this instead do one pass that emits all the files? De-pickling
    # is relatively expensive and we have ~100 pairs (although not all will be useful).
    parser = explain_parser_from_root(es_query)
    writer = tf.python_io.TFRecordWriter(out_path)

    for row, hits in lucene_explains:
        if row['context'] != context or row['language'] != language:
            continue
        if not hits:
            # Probably some sort of query error
            log.debug("No hits for prefix %s", row['prefix'])
            continue
        for page_id, explain in parse_hits(parser, hits):
            example = tf.train.Example(
                features=tf.train.Features(feature=extract_features(row, page_id, explain)))
            writer.write(example.SerializeToString())
    writer.close()


@main.command(
    with_tfrecords, with_equation, with_seed, with_context, with_language,
    with_batch_size(default=16*1024))
def debug_tfrecord(dataset, out_path, equation, seed, context, language, batch_size):
    iterator = dataset.make_initializable_iterator()
    next_batch = iterator.get_next()
    score_op = equation.to_tf(next_batch)

    with tf.Session() as sess, tqdm() as pbar:
        sess.run(tf.global_variables_initializer())
        start = time.time()
        raw_results = []
        for result in tf_run_all(
            sess, iterator.initializer, [score_op, next_batch['meta/explain_value']]
        ):
            pbar.update(result[0].shape[0])
            raw_results.append(result)
        results = np.hstack(raw_results)
        took = time.time() - start
    score = results[0]
    explain_value = results[1]
    condition = ~np.isclose(score, explain_value)
    num_errors = np.sum(condition)
    log.info('Checked %d records and found %d failures in %.4fs', score.shape[0], num_errors, took)


def minimize(
    dataset, out_path, df_source, equation, top_k, restarts, epochs,
    test_size, context, language, seed, make_optimizer, **kwargs
):
    iterator = dataset.make_initializable_iterator()
    next_batch = iterator.get_next()
    score_op = equation.to_tf(next_batch)
    variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
    # For now filter bm25 k1/b from tunables, deploying that is a pain
    variables = [v for v in variables if not v.name.endswith('tfNorm/k1:0') and not v.name.endswith('tfNorm/b:0')]

    # Sort from oldest to newest. Train on oldest, test on newest
    cond = (df_source['context'] == context) & (df_source['language'] == language)
    df_source = df_source[cond].sort_values('dt', ascending=True, inplace=False)
    df_source = df_source[['searchterm', 'clickpage']]
    split_idx = int(len(df_source) * (1 - test_size))
    df_train = df_source.iloc[:split_idx].copy()
    df_test = df_source.iloc[split_idx:].copy()

    with tf.Session() as sess:
        evaluator = AutocompleteEvaluator(
            tf_session=sess,
            data_init_op=iterator.initializer,
            score_op=score_op,
            datasets={
                'test': df_test,
                'train': df_train,
            },
            top_k=top_k,
            variables_ops={var.name: var for var in variables})

        optimizer = make_optimizer(
            tf_session=sess,
            evaluator=evaluator,
            variables=variables,
            seed=seed,
            train_dataset='train',
            **kwargs)

        sess.run(tf.global_variables_initializer())
        evaluator.initialize(next_batch)
        agg_report = optimizer.minimize(restarts=restarts, epochs=epochs)

    agg_report.run_parameters = {
        'context': context,
        'language': language,
        'top_k': top_k,
        'restarts': restarts,
        'epochs': epochs,
        'test_size': test_size,
        'seed': seed,
    }
    pprint.pprint(agg_report.summary)

    with GzipFile(out_path, 'wb') as f:
        pickle.dump(agg_report, f, pickle.HIGHEST_PROTOCOL)
    DELETE_ON_ERROR.append(out_path + '.json')
    with open(out_path + '.json', 'w') as f:
        json.dump(agg_report.to_dict(), f)


def with_minimizer(parser):
    """A with_arg ducktype to reuse shared dependencies

    This is a bad hack, but it works for a single use case.
    """
    loaders = [dep(parser) for dep in [
        with_tfrecords,
        with_resample,  # TODO: with_tfrecords needs deps too
        with_batch_size(default=16*1024),
        with_source_dataset,
        with_equation,
        with_top_k,
        with_restarts,
        with_epochs,
        with_test_size,
        with_context,
        with_language,
        with_seed,
    ]]

    def fn(args):
        for loader in loaders:
            if loader is not None:
                args = loader(args)
        arg_names = ['dataset', 'out_path', 'df_source', 'equation',
                     'top_k', 'restarts', 'epochs', 'test_size',
                     'context', 'language', 'seed']
        minimize_fn = partial(minimize, *[args[k] for k in arg_names])
        return dict(args, minimize=minimize_fn)

    return fn


@main.command(with_minimizer)
def hyperopt(minimize, **kwargs):
    minimize(HyperoptOptimizer)


@main.command(
    with_tfrecords, with_resample, with_batch_size(default=16*1024), with_source_dataset,
    with_equation, with_top_k, with_test_size, with_context, with_language, with_seed,
    with_train_report,
)
def eval_model(
    dataset, out_path, resample, batch_size, df_source, equation, top_k, test_size,
    context, language, seed, train_report,
):
    iterator = dataset.make_initializable_iterator()
    next_batch = iterator.get_next()
    score_op = equation.to_tf(next_batch)
    variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
    variables_by_name = {var.name: var for var in variables}

    # Sort from oldest to newest. test on second half of data
    cond = (df_source['context'] == context) & (df_source['language'] == language)
    df_source = df_source[cond].sort_values('dt', ascending=True, inplace=False)
    df_source = df_source[['searchterm', 'clickpage']]
    assert 0 < test_size < 1  # TODO: argument handling bounds checking
    split_idx = int(len(df_source) * (1 - test_size))
    df_test = df_source.iloc[split_idx:].copy()

    with tf.Session() as sess:
        evaluator = AutocompleteEvaluator(
            tf_session=sess,
            data_init_op=iterator.initializer,
            score_op=score_op,
            datasets={'test': df_test},
            top_k=top_k,
            variables_ops=variables_by_name)

        sess.run(tf.global_variables_initializer())
        evaluator.initialize(next_batch)
        # Assign best values
        sess.run([variables_by_name[k].assign(v) for k, v in train_report.best_report.variables.items()])
        final_report = evaluator.evaluate()

    print('Initial report:')
    pprint.pprint(evaluator.initial_report.summary)
    print('After Training:')
    pprint.pprint(final_report.summary)


@main.command(
    with_tfrecords, with_resample, with_batch_size(default=16*1024), with_source_dataset,
    with_equation, with_top_k, with_test_size, with_context, with_language, with_seed,
    with_train_report(required=False),
    with_arg('--sensitivity-width', dest='width', type=positive_int, default=20, required=False),
)
def analyze_sensitivity(
    dataset, out_path, resample, batch_size, df_source, equation, top_k, test_size,
    context, language, seed, train_report, width,
):
    iterator = dataset.make_initializable_iterator()
    next_batch = iterator.get_next()
    score_op = equation.to_tf(next_batch)
    variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
    variables_by_name = {var.name: var for var in variables}

    # Sort from oldest to newest. test on second half of data
    cond = (df_source['context'] == context) & (df_source['language'] == language)
    df_source = df_source[cond].sort_values('dt', ascending=True, inplace=False)
    df_source = df_source[['searchterm', 'clickpage']]
    assert 0 < test_size < 1  # TODO: argument handling bounds checking
    split_idx = int(len(df_source) * (1 - test_size))
    df_test = df_source.iloc[split_idx:].copy()

    with tf.Session() as sess:
        evaluator = AutocompleteEvaluator(
            tf_session=sess,
            data_init_op=iterator.initializer,
            score_op=score_op,
            datasets={'test': df_test},
            top_k=top_k,
            variables_ops=variables_by_name)

        analyzer = SensitivityAnalyzer(
            tf_session=sess,
            evaluator=evaluator,
            variables=variables,
            width=width)

        sess.run(tf.global_variables_initializer())
        if train_report is not None:
            sess.run([variables_by_name[k].assign(v) for k, v in train_report.best_report.variables.items()])
        evaluator.initialize(next_batch)
        sensitivity_report = analyzer.evaluate()

    with GzipFile(out_path, 'wb') as f:
        pickle.dump(sensitivity_report, f, pickle.HIGHEST_PROTOCOL)
    DELETE_ON_ERROR.append(out_path + '.json')
    with open(out_path + '.json', 'w') as f:
        f.write(json.dumps(sensitivity_report.to_dict()))


if __name__ == "__main__":
    logging.basicConfig(level='INFO')
    main()