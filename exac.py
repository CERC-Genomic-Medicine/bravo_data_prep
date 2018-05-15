#!/usr/bin/env python2

import itertools
import json
import os
import pymongo
import pysam
import gzip
import random
import os
import boltons.cacheutils

from flask import Flask, Response, request, session, g, redirect, url_for, abort, render_template, flash, jsonify, make_response, send_file, Blueprint
from flask_compress import Compress
from flask_errormail import mail_on_500
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user

from collections import defaultdict, Counter
from multiprocessing import Process
import multiprocessing
import glob
import traceback
import time
import sys
import functools
import contextlib

from parsing import *
import lookups
from lookups import IntervalSet, TranscriptSet
from utils import *
from base_coverage import CoverageHandler
import auth

import socket
from ipaddress import ip_network, AddressValueError
import re

import pysam
import io
import sequences
from datetime import timedelta

bp = Blueprint('bp', __name__, template_folder='templates', static_folder='static')

app = Flask(__name__)
app.config.from_object('flask_config.BravoFreeze5GRCh38Config')
if 'GVS_URL_PREFIX' in os.environ: app.config['URL_PREFIX'] = os.environ['GVS_URL_PREFIX']
if 'BRAVO_ADMIN_MODE' in os.environ: app.config['ADMIN'] = True if os.environ['BRAVO_ADMIN_MODE'].lower() == 'true' else False
mail_on_500(app, app.config['ADMINS'])
app.config['COMPRESS_LEVEL'] = 2 # Since we don't cache, faster=better
Compress(app)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 5 # 5 second browser cache timeout
app.config['TEMPLATES_AUTO_RELOAD'] = True

MAX_REGION_LENGTH = int(350e3) # Longer than TTN (305kb), short enough to perform okay.

def get_db(new_connection=False):
    # Only use the database within a request context! Something about threads/forks.
    # See <https://jira.mongodb.org/browse/PYTHON-961>
    # Note: I just added `connect=False`, so maybe we don't need this function anymore (unless used with new_connection=True)
    if new_connection:
        client = pymongo.MongoClient(host=app.config['MONGO']['host'], port=app.config['MONGO']['port'], connect=False)
    else:
        client = get_db._mongo_client
    return client[app.config['MONGO']['name']]

get_db._mongo_client = pymongo.MongoClient(host=app.config['MONGO']['host'], port=app.config['MONGO']['port'], connect=False)
sequencesClient = sequences.SequencesClient(app.config['IGV_CRAM_DIRECTORY'], app.config['IGV_REFERENCE_PATH'], app.config['IGV_CACHE_DIRECTORY'], app.config['IGV_CACHE_COLLECTION'], 100)

@boltons.cacheutils.cached({})
def get_autocomplete_strings():
    autocomplete_strings = get_db().genes.distinct('gene_name')
    autocomplete_strings.extend(get_db().genes.distinct('other_names', {'other_names': {'$ne': None}}))
    return sorted(set(autocomplete_strings))

@boltons.cacheutils.cached({})
def get_coverage_handler():
    return CoverageHandler(app.config['BASE_COVERAGE'])

def get_tabix_file_contig_pairs(tabix_filenames):
    filename_contig_pairs = []
    for tabix_filename in tabix_filenames:
        with pysam.Tabixfile(tabix_filename) as tabix_file:
            for contig in tabix_file.contigs:
                filename_contig_pairs.append((tabix_filename, contig))
    def _sort_key(pair):
        (filename, contig) = pair
        if contig.startswith('chr'): contig = contig[3:]
        if contig.isdigit(): return int(contig)
        return 0 # for X/Y/MT
    filename_contig_pairs.sort(key=_sort_key) # Sort from large -> small chromosomes
    return filename_contig_pairs

def get_records_from_tabix_contig(tabix_filename, contig, record_parser):
    start_time = time.time()
    with pysam.Tabixfile(tabix_filename) as tabix_file:
        record_i = 0 # in case record_parser never yields anything.
        for record_i, parsed_record in enumerate(record_parser(itertools.chain(tabix_file.header, tabix_file.fetch(contig, 0, 10**10, multiple_iterators=True))), start=1):
            yield parsed_record

            if record_i % int(1e6) == 0:
                print("Loaded {:11,} records in {:6,} seconds from contig {!r:6} of {!r}".format(record_i, int(time.time()-start_time), contig, tabix_filename))
    print("Loaded {:11,} records in {:6,} seconds from contig {!r:6} of {!r}".format(record_i, int(time.time()-start_time), contig, tabix_filename))


def _load_variants_from_tabix_file_and_contig(args, collection_name, parser):
    tabix_file, contig = args
    db = get_db(new_connection = True)
    collection = db[collection_name]
    variants_generator = get_records_from_tabix_contig(tabix_file, contig, parser)
    try:
        collection.insert(variants_generator, w = 0)
    except pymongo.errors.InvalidOperation:
        pass  # handle error when variant_generator is empty


def load_variants_file():
    if len(app.config['SITES_VCFS']) == 0:
        raise IOError("No vcf file found.")

    db = get_db()
    db.variants.drop()
    print("Dropped db.variants")

    file_contig_pairs = get_tabix_file_contig_pairs(app.config['SITES_VCFS'])
    with contextlib.closing(multiprocessing.Pool(app.config['LOAD_DB_PARALLEL_PROCESSES'])) as pool:
        # workaround for Pool.map() from <http://stackoverflow.com/a/1408476/1166306>
        pool.map_async(functools.partial(_load_variants_from_tabix_file_and_contig, collection_name = 'variants', parser = get_variants_from_sites_vcf), file_contig_pairs).get(9999999)

    # TODO: use db.variants.create_indexes([pymongo.operations.IndexModel(key) for key in 'xpos xstop rsids filter'.split()])
    for key in ('xpos', 'xstop', 'rsids', 'filter'):
        print 'creating index on {} in db.{}'.format(key, 'variants')
        db.variants.create_index(key)


def load_custom_variants_file(collection_name, vcfs):
    db = get_db()
    if collection_name in db.collection_names():
        raise Exception("{} collection already exists.".format(collection_name))
    file_contig_pairs = get_tabix_file_contig_pairs(vcfs)
    with contextlib.closing(multiprocessing.Pool(app.config['LOAD_DB_PARALLEL_PROCESSES'])) as pool:
        pool.map_async(functools.partial(_load_variants_from_tabix_file_and_contig, collection_name = collection_name, parser = get_variants_from_sites_vcf_without_annotation), file_contig_pairs).get(9999999)
    collection = db[collection_name]
    for key in ('xpos', 'xstop', 'filter'):
        print 'creating index on {} in db.{}'.format(key, collection_name)
        collection.create_index(key)


def load_percentiles(vcfs):
    with contextlib.closing(multiprocessing.Pool(app.config['LOAD_DB_PARALLEL_PROCESSES'])) as pool:
        pool.map_async(_load_percentiles_from_vcf, vcfs).get(9999999)


def _load_percentiles_from_vcf(vcf):
    db = get_db()
    n_variants = 0
    n_matched = 0
    n_modified = 0
    with gzip.GzipFile(vcf, 'r') as ivcf:
        start_time = time.time()
        requests = []
        for variant in get_variants_from_sites_vcf_only_percentiles(ivcf):
            requests.append(pymongo.operations.UpdateOne(
                {'xpos': variant['xpos'], 'ref': variant['ref'], 'alt': variant['alt']},
                {'$set': {'quality_metrics_percentiles': variant['percentiles']}},
                upsert = False))
            n_variants += 1
            if n_variants % 1000000 == 0:
                res = db.variants.bulk_write(requests, ordered = False)
                n_matched += res.matched_count
                n_modified += res.modified_count
                requests = []
                print 'VCF {}. Processed {} variant(s) in {} second(s), {} matched, {} modified.'.format(vcf, n_variants, int(time.time() - start_time), n_matched, n_modified) 
        if len(requests) > 0:
            res = db.variants.bulk_write(requests, ordered = False)
            n_matched += res.matched_count
            n_modified += res.modified_count
            print 'Finished. VCF {}. Processed {} variant(s) in {} second(s), {} matched, {} modified.'.format(vcf, n_variants, int(time.time() - start_time), n_matched, n_modified)




def create_sequence_cache(collection = None):
    db = get_db()
    if collection is None:
        collection = app.config['IGV_CACHE_COLLECTION']
    sequences.SequencesClient.create_cache_collection_and_index(db, collection)


def require_agreement_to_terms_and_store_destination(func):
    """
    This decorator for routes checks that the user is logged in and has agreed to the terms.
    If they haven't, their intended destination is stored and they're sent to get authorized.
    If such check is not mandatory (i.e. not required), then set GOOGLE_AUTH and TERMS flags in configuration file to False.
    I think that it has to be placed AFTER @app.route() so that it can capture `request.path`.
    """
    # inspired by <https://flask-login.readthedocs.org/en/latest/_modules/flask_login.html#login_required>
    @functools.wraps(func)
    def decorated_view(*args, **kwargs):
        if app.config['GOOGLE_AUTH']:
            if current_user.is_anonymous:
                session['original_destination'] = request.path
                return redirect(url_for('.get_authorized'))
            if app.config['TERMS'] and (not hasattr(current_user, 'agreed_to_terms') or not current_user.agreed_to_terms):
                session['original_destination'] = request.path
                return redirect(url_for('.terms_page'))
        return func(*args, **kwargs)
    return decorated_view


def _log(message = ''):
    url = request.full_path.rstrip('?')
    if url.startswith(app.config['URL_PREFIX']): url = url[len(app.config['URL_PREFIX']):]
    print('{}  {}{}'.format(current_user, url, message))


def _err():
    url = request.full_path.rstrip('?')
    if url.startswith(app.config['URL_PREFIX']): url = url[len(app.config['URL_PREFIX']):]
    error = traceback.format_exc()
    if request.form: print('Failed on {} with form {} and error:\n{}'.format(url, request.form, error))
    else: print('Failed on {} with error:\n{}'.format(url, error))

@bp.route('/')
def homepage():
    return render_template('homepage.html')


@bp.route('/api/autocomplete')
def autocomplete():
    db = get_db()
    query = request.args.get('query', '')
    suggestions = lookups.get_awesomebar_suggestions(get_autocomplete_strings(), query, db)
    _log('  =>  {} results'.format(len(suggestions)))
    return jsonify([{'value': s} for s in sorted(suggestions)])


@bp.route('/awesome')
def awesome():
    db = get_db()
    query = request.args.get('query')
    if query is None:
        return redirect(url_for('.homepage'))
    datatype, redirect_args = lookups.get_awesomebar_result(db, query)
    _log('  =>  {}_page({})'.format(datatype, redirect_args))
    return redirect(url_for('.{}_page'.format(datatype), **redirect_args))


@bp.route('/profile', methods=['GET', 'POST'])
@require_agreement_to_terms_and_store_destination
def user_profile_page():
    try:
        _log()
        error = None
        success = None
        if request.method == 'POST':
            enabled_api = False if request.form.get('enabled_api', '').lower() != 'on' else True
            no_newsletters = False if request.form.get('no_newsletters', '').lower() != 'on' else True
            if error is None:
                db = get_db()
                result = db.users.update_one({"user_id": current_user.get_id()}, {"$set": {"enabled_api": enabled_api, "no_newsletters": no_newsletters}})
                success = True
                current_user.enabled_api = enabled_api
                current_user.no_newsletters = no_newsletters
        return render_template('user_profile.html', error = error, success = success)
    except: _err(); abort(500)

@bp.route('/administration', methods = ['GET', 'POST'])
@require_agreement_to_terms_and_store_destination
def administration_page():
    if not current_user.admin:
        abort(404)
    try:
        _log()
        error = None
        success = None
        return render_template('administration.html', error = error, success = success)
    except: _err(); abort(500)

@bp.route('/administration/users', methods = ['POST'])
@require_agreement_to_terms_and_store_destination
def administration_users_api():
    if not current_user.admin:
        abort(404)
    db = get_db()
    args = json.loads(request.form['args'])
 
    mongo_projection = {
        '_id': False, 
        'username': True,
        'email': True, 
        'enabled_api': {'$ifNull': ['$enabled_api', False]},
        'no_newsletters': {'ifNull': ['no_newsletters', False]},
    }

    mongo_sort = {}
    for order in args['order']:
        mongo_sort[args['columns'][order['column']]['name']] = pymongo.ASCENDING if order['dir'] == 'asc' else pymongo.DESCENDING

    mongo_filter = {}
    for column in args['columns']:
        if column['search']['value'].lstrip():
            if column['name'] not in {'enabled_api', 'no_newsletters'}:
                mongo_filter[column['name']] = {'$regex': '.*{}.*'.format(column['search']['value'].lstrip())}
            elif any(column['search']['value'] == x for x in ['Yes', 'No']):
                mongo_filter[column['name']] = True if column['search']['value'] == 'Yes' else {'$ne': True}

    try:
        n_total_users = db.users.find().count()
        n_filtered_users = db.users.find(mongo_filter).count() if mongo_filter else n_total_users
        users = list(db.users.aggregate([ {'$match': mongo_filter}, {'$sort': mongo_sort}, {'$skip': args['start']}, {'$limit': args['length']}, {'$project': mongo_projection} ]))
        response = { 'recordsFiltered': n_filtered_users, 'recordsTotal': n_total_users, 'data': users , 'draw': args['draw'] }
        return jsonify(response)
    except: _err(); abort(500)


@bp.route('/variant/<variant_id>')
@require_agreement_to_terms_and_store_destination
def variant_page(variant_id):
    db = get_db()
    try:
        _log()
        variant = lookups.get_variant_by_variant_id(db, variant_id, default_to_boring_variant = False)
        if not variant: return not_found_page('The requested variant {!s} could not be found.'.format(variant_id))
     
        pop_names = {k + '_AF': '1000G ' + v for k, v in {'AFR':'African', 'AMR':'American', 'EAS':'East Asian', 'EUR':'European', 'SAS':'South Asian'}.items()}
        if 'pop_afs' in variant:
            variant['pop_afs'] = {pop_names.get(k, k): v for k, v in variant['pop_afs'].items()}
        else:
            variant['pop_afs'] = { x: None  for x in pop_names.itervalues() }
        variant['pop_afs'][app.config['DATASET_NAME']] = variant['allele_freq']

        consequence_drilldown = ConsequenceDrilldown.from_variant(variant)
        gene_for_top_csq, top_HGVSs = ConsequenceDrilldown.get_top_gene_and_HGVSs(consequence_drilldown)
        consequence_drilldown_columns = ConsequenceDrilldown.split_into_two_columns(consequence_drilldown)

        base_coverage = get_coverage_handler().get_coverage_for_intervalset(
            IntervalSet.from_xstart_xstop(variant['xpos'], variant['xpos']+len(variant['ref'])-1))
        
        metrics = lookups.get_metrics(db)
        variant['quality_metrics']['QUAL'] = variant['site_quality']

        lookups.remove_some_extraneous_information(variant)

        return render_template(
            'variant.html',
            variant=variant,
            base_coverage=base_coverage,
            consequences=consequence_drilldown,
            consequence_columns=consequence_drilldown_columns,
            any_covered=bool(base_coverage),
            metrics=metrics,
            top_HGVSs=top_HGVSs,
            gene_for_top_csq=gene_for_top_csq,
        )
    except: _err(); abort(500)


@bp.route('/gene/<gene_id>')
@require_agreement_to_terms_and_store_destination
def gene_page(gene_id):
    db = get_db()
    try:
        gene = lookups.get_gene(db, gene_id)
        if not gene: return not_found_page('The requested gene {!s} could not be found.'.format(gene_id))
        _log('   ({})'.format(gene.get('gene_name')))
        intervalset = IntervalSet.from_gene(db, gene_id)
        genes = TranscriptSet.from_gene(db, gene_id).genes
        return render_template(
            'gene.html',
            intervalset=intervalset, genes=genes, csq=Consequence.as_obj,
            gene=gene,
        )
    except:_err(); abort(500)

@bp.route('/transcript/<transcript_id>')
@require_agreement_to_terms_and_store_destination
def transcript_page(transcript_id):
    db = get_db()
    try:
        _log()
        transcript = lookups.get_transcript(db, transcript_id)
        if not transcript: return not_found_page('The requested transcript {!s} could not be found.'.format(transcript_id))
        gene = lookups.get_gene(db, transcript['gene_id'])
        intervalset = IntervalSet.from_transcript(db, transcript_id)
        genes = TranscriptSet.from_transcript(db, transcript_id).genes
        return render_template(
            'transcript.html',
            intervalset=intervalset, genes=genes, csq=Consequence.as_obj,
            gene=gene,
            transcript=transcript,
        )
    except:_err(); abort(500)

@bp.route('/region/<chrom>-<start>-<stop>')
@require_agreement_to_terms_and_store_destination
def region_page(chrom, start, stop):
    db = get_db()
    try:
        _log()
        try:
            start = int(start)
        except:
            return bad_request_page('The start position {!s} is not integer.'.format(start))
        try: 
            stop = int(stop)
        except:
            return bad_request_page('The stop position {!s} is not integer.'.format(stop))
        if start > stop:
            return bad_request_page("The region '{chrom}-{start}-{stop}' stops before it starts. Did you mean '{chrom}-{stop}-{start}'?".format(chrom = chrom, start = start, stop = stop))
        if stop-start > MAX_REGION_LENGTH:
            return bad_request_page("The region '{chrom}-{start}-{stop}' is {:,} bases. We only accept regions shorter than {:,} bases.".format(stop - start, MAX_REGION_LENGTH, chrom = chrom, start = start, stop = stop))
        if start == stop:
            start -= 20
            stop += 20
        intervalset = IntervalSet.from_chrom_start_stop(chrom, start, stop)
        genes = TranscriptSet.from_chrom_start_stop(db, chrom, start, stop).genes
        return render_template(
            'region.html',
            intervalset = intervalset, genes = genes, csq = Consequence.as_obj,
        )
    except: _err(); abort(500)

@bp.route('/download/gene/<gene_id>')
@require_agreement_to_terms_and_store_destination
def download_gene_variants(gene_id):
    db = get_db()
    try:
        intervalset = IntervalSet.from_gene(get_db(), gene_id)
        return _get_variants_csv_for_intervalset(intervalset, '{}.csv'.format(gene_id))
    except:_err(); abort(500)

@bp.route('/download/transcript/<transcript_id>')
@require_agreement_to_terms_and_store_destination
def download_transcript_variants(transcript_id):
    db = get_db()
    try:
        intervalset = IntervalSet.from_transcript(get_db(), transcript_id)
        return _get_variants_csv_for_intervalset(intervalset, '{}.csv'.format(transcript_id))
    except:_err(); abort(500)

@bp.route('/download/region/<chrom>-<start>-<stop>')
@require_agreement_to_terms_and_store_destination
def download_region_variants(chrom, start, stop):
    try:
        start,stop = int(start),int(stop); assert stop-start <= MAX_REGION_LENGTH
        intervalset = IntervalSet.from_chrom_start_stop(chrom, start, stop)
        return _get_variants_csv_for_intervalset(intervalset, 'chr{}-{}-{}.csv'.format(chrom, start, stop))
    except:_err(); abort(500)

def _get_variants_csv_for_intervalset(intervalset, filename):
    _log()
    resp = make_response(lookups.get_variants_csv_str_for_intervalset(get_db(), intervalset))
    resp.headers['Content-Disposition'] = 'attachment; filename={}'.format(filename)
    resp.mimetype='text/csv'
    return resp


@bp.route('/api/summary/gene/<gene_id>')
@require_agreement_to_terms_and_store_destination
def gene_summary_api(gene_id):
    try:
        intervalset = IntervalSet.from_gene(get_db(), gene_id)
        return jsonify(lookups.get_summary_for_intervalset(get_db(), intervalset))
    except:_err(); abort(500)

@bp.route('/api/summary/transcript/<transcript_id>')
@require_agreement_to_terms_and_store_destination
def transcript_summary_api(transcript_id):
    try:
        intervalset = IntervalSet.from_transcript(get_db(), transcript_id)
        return jsonify(lookups.get_summary_for_intervalset(get_db(), intervalset))
    except:_err(); abort(500)

@bp.route('/api/summary/region/<chrom>-<start>-<stop>')
@require_agreement_to_terms_and_store_destination
def region_summary_api(chrom, start, stop):
    try:
        start,stop = int(start),int(stop); assert stop-start <= MAX_REGION_LENGTH
        intervalset = IntervalSet.from_chrom_start_stop(chrom, start, stop)
        return jsonify(lookups.get_summary_for_intervalset(get_db(), intervalset))
    except:_err(); abort(500)

@bp.route('/api/variants/gene/<gene_id>', methods=['POST'])
@require_agreement_to_terms_and_store_destination
def gene_variants_subset_api(gene_id):
    try:
        intervalset = IntervalSet.from_gene(get_db(), gene_id)
        return _get_variants_subset_response_for_intervalset(intervalset)
    except:_err(); abort(500)

@bp.route('/api/variants/transcript/<transcript_id>', methods=['POST'])
@require_agreement_to_terms_and_store_destination
def transcript_variants_subset_api(transcript_id):
    try:
        intervalset = IntervalSet.from_transcript(get_db(), transcript_id)
        return _get_variants_subset_response_for_intervalset(intervalset)
    except:_err(); abort(500)

@bp.route('/api/variants/region/<chrom>-<start>-<stop>', methods=['POST'])
@require_agreement_to_terms_and_store_destination
def region_variants_subset_api(chrom, start, stop):
    try:
        start,stop = int(start),int(stop); assert stop-start <= MAX_REGION_LENGTH
        intervalset = IntervalSet.from_chrom_start_stop(chrom, start, stop)
        return _get_variants_subset_response_for_intervalset(intervalset)
    except:_err(); abort(500)

def _get_variants_subset_response_for_intervalset(intervalset):
    db = get_db()
    args = json.loads(request.form['args'])
    assert isinstance(args['draw'], int)
    filter_info = json.loads(request.form['filter_info'])
    _log('   '+str(filter_info))
    ret = lookups.get_variants_subset_for_intervalset(
        db, intervalset, args['columns'], args['order'], filter_info, skip=args['start'], length=args['length']
    )
    ret['draw'] = args['draw']
    return jsonify(ret)

@bp.route('/api/coverage/gene/<gene_id>')
@require_agreement_to_terms_and_store_destination
def gene_coverage_api(gene_id):
    try:
        intervalset = IntervalSet.from_gene(get_db(), gene_id)
        return jsonify(get_coverage_handler().get_coverage_for_intervalset(intervalset))
    except:_err(); abort(500)

@bp.route('/api/coverage/transcript/<transcript_id>')
@require_agreement_to_terms_and_store_destination
def transcript_coverage_api(transcript_id):
    try:
        intervalset = IntervalSet.from_transcript(get_db(), transcript_id)
        return jsonify(get_coverage_handler().get_coverage_for_intervalset(intervalset))
    except:_err(); abort(500)

@bp.route('/api/coverage/region/<chrom>-<start>-<stop>')
@require_agreement_to_terms_and_store_destination
def region_coverage_api(chrom, start, stop):
    try:
        start,stop = int(start),int(stop); assert stop-start <= MAX_REGION_LENGTH
        intervalset = IntervalSet.from_chrom_start_stop(chrom, start, stop)
        return jsonify(get_coverage_handler().get_coverage_for_intervalset(intervalset))
    except:_err(); abort(500)

@bp.route('/multi_variant_rsid/<rsid>')
@require_agreement_to_terms_and_store_destination
def multi_variant_rsid_page(rsid):
    db = get_db()
    try:
        _log()
        variants = lookups.get_variants_by_rsid(db, rsid)
        if variants is None or len(variants) == 0:
            return not_found_page("There are no variants with the rsid '{}'".format(rsid))
        return not_found_page('There are multiple variants at the location of rsid {}: {}'.format(
            rsid,
            ', '.join('{chrom}-{pos}-{ref}-{alt}'.format(**variant) for variant in variants)))
    except:_err(); abort(500)

@bp.route('/not_found/<message>')
def not_found_page(message):
    return render_template('not_found.html', message = message), 404

@bp.route('/bad_request/<message>')
def bad_request_page(message):
    return render_template('bad_request.html', message = message), 400


@bp.route('/download')
@require_agreement_to_terms_and_store_destination
def download_page():
    _log()
    return render_template('download.html')


@bp.route('/download/all')
@require_agreement_to_terms_and_store_destination
def download_full_vcf():
    _log()
    try:
        return make_response(send_file(app.config['DOWNLOAD_ALL_FILEPATH'], as_attachment=True, mimetype='application/gzip'))
    except:_err(); abort(500)


@bp.route('/about')
def about_page():
    _log()
    return render_template('about.html')


@bp.route('/terms')
def terms_page():
    _log()
    if app.config['GOOGLE_AUTH'] and app.config['TERMS']:
        return render_template('terms.html')
    abort(404)


@bp.route('/help')
def help_page():
    _log()
    return render_template('help.html')


@bp.route('/variant/<variant_id>/reads')
@require_agreement_to_terms_and_store_destination
def variant_bams(variant_id):
    db = get_db()
    try:
        _log()
        start_time = time.time()
        response = sequencesClient.get_samples(db, variant_id)
        print 'Done preparing samples. Took %s seconds' % (time.time() - start_time)
        if response is None:
            response = { 'names': [] }
        return jsonify(response)
    except: _err(); abort(500)


@bp.route('/variant/<variant_id>/<sample_id>.bam.bai')
@require_agreement_to_terms_and_store_destination
def test_bai(variant_id, sample_id):
    db = get_db()
    try:
        _log()
        start_time = time.time()
        file_path = sequencesClient.get_bai(db, variant_id, sample_id)
        if file_path is None: _err(); abort(500)
        print 'Done preparing BAM and BAI. Took %s seconds' % (time.time() - start_time)
        return make_response(send_file(file_path, as_attachment = False)) 
    except: _err(); abort(500)


@bp.route('/variant/<variant_id>/<sample_id>.bam')
@require_agreement_to_terms_and_store_destination
def test_bam(variant_id, sample_id):
    db = get_db()
    try:
        start_time = time.time()
        range_header = request.headers.get('Range', None)
        m = re.search('(\d+)-(\d*)', range_header)
        result = sequencesClient.get_bam(db, variant_id, sample_id, m.group(1), m.group(2))
        if result is None: _err(); abort(500)
        response = Response(result['data'], 206, mimetype = "application/octet-stream .bam", direct_passthrough = True)
        response.headers['Content-Range'] = 'bytes {0}-{1}/{2}'.format(result['start'], result['end'], result['size'])
        print 'Prepared BAM for sending. Took %s seconds' % (time.time() - start_time)
        return response
    except: _err(); abort(500)



# OAuth2
google_sign_in = auth.GoogleSignIn(app)

lm = LoginManager(app)
lm.login_view = 'bp.homepage'


class User(UserMixin):
    "A user's id is their email address."
    def __init__(self, username = None, email = None, agreed_to_terms = False, picture = None, enabled_api = False, google_client_id = None, no_newsletters = False, admin = False):
        self.username = username
        self.email = email
        self.agreed_to_terms = agreed_to_terms
        self.picture = picture
        self.enabled_api = enabled_api
        self.google_client_id = google_client_id
        self.no_newsletters = no_newsletters
        self.admin = admin
    def get_id(self):
        return self.email
    def __str__(self):
        return "<{}>".format(self.email or None)
    def __repr__(self):
        return "<User email={!r} username={!r} terms={!r} admin={!r}>".format(self.email, self.username, self.agreed_to_terms, self.admin)


def encode_user(user):
    return { '_type': 'User', 
             'user_id': user.get_id(), 
             'username': user.username, 
             'email': user.email, 
             'agreed_to_terms': user.agreed_to_terms, 
             'picture': user.picture, 
             'enabled_api': user.enabled_api, 
             'google_client_id': user.google_client_id, 
             'no_newsletters': user.no_newsletters }


def decode_user(document):
    return User(username = document['username'],
                email = document['email'],
                agreed_to_terms = document['agreed_to_terms'],
                picture = document.get('picture', None),
                enabled_api = document.get('enabled_api', False),
                google_client_id = document.get('google_client_id', None),
                no_newsletters = document.get('no_newsletters', False))


@lm.user_loader
def load_user(id):
    db = get_db()
    try:
        document = db.users.find_one({'user_id': id, '_type': 'User'}, projection = {'_id': False})
        if document:
            user = decode_user(document)
            # check if user is admin and admin mode is enabled
            if app.config['ADMIN'] is True and user.email in app.config['ADMINS']:
                if app.config['PROXY'] is True:
                    x_forwarded_for = request.headers.get('X-Forwarded-For', '').split(',')
                    ip = x_forwarded_for[-1].strip() if len(x_forwarded_for) > 0 else ''
                else:
                    ip = request.remote_addr
                try:
                    network = ip_network(ip).supernet(new_prefix = 16)
                    if any(network == ip_network(x) for x in app.config['ADMIN_ALLOWED_IP']):
                        user.admin = True
                except AddressValueError:
                    user.admin = False
            return user
    except:
        pass
    return None


@bp.route('/agree_to_terms')
def agree_to_terms():
    "this route is for when the user has clicked 'I agree to the terms'."
    _log()
    if app.config['GOOGLE_AUTH'] and app.config['TERMS']: 
        if not current_user.is_anonymous:
            db = get_db()
            current_user.agreed_to_terms = True
            result = db.users.update_one({"user_id": current_user.get_id()}, {"$set": {"agreed_to_terms": current_user.agreed_to_terms}})     
        return redirect(session.pop('original_destination', url_for('.homepage')))
    abort(404)


@bp.route('/login_with_google')
def login_with_google():
    "this route is for the login button"
    _log()
    if app.config['GOOGLE_AUTH']:
        session['original_destination'] = url_for('.homepage')
        return redirect(url_for('.get_authorized'))
    abort(404)


@bp.route('/logout')
def logout():
    _log()
    if app.config['GOOGLE_AUTH']:
        logout_user()
        return redirect(url_for('.homepage'))
    abort(404)


@bp.route('/get_authorized')
def get_authorized():
    _log()
    if app.config['GOOGLE_AUTH']:
        if current_user.is_anonymous:
            return google_sign_in.authorize()
        return redirect(session.pop('original_destination', url_for('.homepage')))
    abort(404)


@bp.route('/callback/google')
def oauth_callback_google():
    _log()
    if not app.config['GOOGLE_AUTH']:
        abort(404)
    username, email, picture = google_sign_in.callback() # oauth.callback reads request.args.
    if email is None:
        flash('Authentication failed.')
        return redirect(url_for('.homepage'))

    db = get_db()

    if app.config['EMAIL_WHITELIST']:
        document = db.whitelist.find_one({'user_id': email}, projection = {'_id': False})
        if not document:
            flash('Authentication failed.')
            return redirect(url_for('.homepage'))

    document = db.users.find_one({'user_id': email}, projection = {'_id': False})
    if document:
        user = decode_user(document)
        if picture and picture != user.picture:
            result = db.users.update_one({"user_id": user.get_id()}, {"$set": {"picture": picture}})
            user.picture = picture
    else:
        user = User(email = email, username = username or email.split('@')[0], picture = picture)
        db.users.insert(encode_user(user))

    login_user(user, remember = True, duration = timedelta(days = 1))
    return redirect(session.pop('original_destination', url_for('.homepage')))


@bp.after_request
def apply_caching(response):
    # prevent click-jacking vulnerability identified by BITs
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response

app.register_blueprint(bp, url_prefix = app.config['URL_PREFIX'])

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0', help='the hostname to use to access this server')
    parser.add_argument('--port', type=int, default=5000, help='an integer for the accumulator')
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=True)
