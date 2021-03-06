from subprocess import call
import os
import gc
import glob
import codecs
from itertools import product, chain
from collections import Counter
from os.path import basename
from opencricket.chart.sentence_parser import SentenceParser
import elasticsearch
from elasticsearch import Elasticsearch
from elasticsearch import helpers
from opencricket.config import es_config
from opencricket.config import word_config
from nltk.grammar import Nonterminal
from collections import defaultdict

OPEN_CRICKET_INDEX = 'opencricket'

EXPANSIONS = 'expansions'
DYNAMIC_EXPANSIONS = 'dynamic_expansions'
SYNTAX = 'syntax'


class Productions:
    def __init__(self, es=Elasticsearch()):
        self.es = es

    def productions(self, expansions_dir):
        # TODO While producing expansions, do Map & Reduce instead of Iteration
        result = []
        parser = SentenceParser('')
        possible_filters = word_config.expandable_filters + list(word_config.match_clauses.keys()) + \
                           ['word_this_last', 'word_against', 'word_captain',
                            'words_wicket_keeper', 'words_single_innings',
                            'words_single_match', 'words_single_series',
                            'words_single_year', 'words_single_ground', 'word_batting',
                            'word_wkt_order']
        expansion_files = list(
            os.path.splitext(basename(f))[0] for f in glob.iglob(os.path.join(expansions_dir, '*.txt')))
        for stats_parser in parser.cfg_parsers:
            # if not str(stats_parser.start()) == 'matches': continue
            root = str(stats_parser.start())
            root_productions = stats_parser.productions(lhs=Nonterminal(root))
            result_productions = []
            syntax_expansions = {}
            dynamic_expansions = defaultdict(list)
            for p in root_productions:
                syntax = str(p)
                syntax_split = self.strip_permutation(syntax.split(' -> ')[1], possible_filters)
                if syntax_split is None: continue
                if not self.contains(result_productions, syntax_split): result_productions.append(syntax_split)
            for key in stats_parser._leftcorner_words.keys():
                if str(key).startswith('word_'):
                    syntax_expansions[str(key)] = list(stats_parser._leftcorner_words[key])[0]
            for s in stats_parser._lhs_index:
                key = str(s).split(' -> ')[0]
                if key == root or any(char.isdigit() for char in key) or key.startswith('word_') or any(
                        key.startswith(f) for f in expansion_files):
                    continue
                for dynamic_production in stats_parser.productions(lhs=s):
                    if key.startswith('words_'):
                        syntax_expansions[str(s).split(' -> ')[0]] = ' '.join(list(dynamic_production._rhs))
                    else:
                        dynamic_expansions[str(s).split(' -> ')[0]].append(' '.join(map(str, dynamic_production.rhs())))
            result.append({root: {SYNTAX: result_productions,
                                  EXPANSIONS: syntax_expansions,
                                  DYNAMIC_EXPANSIONS: dynamic_expansions
                                  }})
        return result

    def explode(self, expansions_dir, exploded_dir):
        reference_expansions = {}
        for filename in glob.iglob(os.path.join(expansions_dir, '*.txt')):
            with codecs.open(filename, encoding='utf-8') as f:
                reference_expansions[os.path.splitext(basename(f.name))[0]] = f.read().splitlines()
        productions = self.productions(expansions_dir)
        for production in productions:
            for key, syntax in production.items():
                exploded_filename = key
                if os.path.exists(os.path.join(exploded_dir, exploded_filename)): os.remove(
                    os.path.join(exploded_dir, exploded_filename))
                syntax_list = syntax[SYNTAX]
                static_expansions = syntax[EXPANSIONS]
                dynamic_expansions = syntax[DYNAMIC_EXPANSIONS]
                for expansion_key, static_expansion in static_expansions.items():
                    reference_expansions[expansion_key] = [static_expansion]
                for expansion_key, dynamic_expansion_list in dynamic_expansions.items():
                    reference_expansions[expansion_key] = []
                    for dynamic_expansion in dynamic_expansion_list:
                        tmp = ' '.join(['%s'] * len(dynamic_expansion.split()))
                        final_items = list(
                            reference_expansions[item if (item in reference_expansions or item.startswith('word_') or
                                                          item.startswith('words_')) else item.split('_')[0]] for item
                            in dynamic_expansion.split())
                        reference_expansions[expansion_key].append(list(tmp % a for a in list(product(*final_items))))
                    reference_expansions[expansion_key] = list(chain(*reference_expansions[expansion_key]))
                for s in syntax_list:
                    tmp = ' '.join(['%s'] * len(s.split()))
                    final_items = list(reference_expansions[item] for item in s.split())
                    with codecs.open(os.path.join(exploded_dir, exploded_filename), 'a', 'utf-8') as f:
                        f.write('\n'.join([tmp % a for a in list(product(*final_items))]) + '\n')


    def create_index(self):
        self.es.indices.create(index=OPEN_CRICKET_INDEX, body=es_config.index_settings)
        parser = SentenceParser('')
        for doc_type in list(map(str, (p.start() for p in parser.cfg_parsers))):
            self.put_mapping(doc_type)

    def put_mapping(self, doc_type):
        self.es.indices.put_mapping(index=OPEN_CRICKET_INDEX, doc_type=doc_type,
                                        body=es_config.type_mapping(doc_type))

    def delete_documents(self, doc_type):
        self.es.delete_by_query(index=OPEN_CRICKET_INDEX, doc_type=doc_type,
                                        body=es_config.delete_documents)

    def load_index(self, exploded_dir):
        for filename in glob.iglob(os.path.join(exploded_dir, '*')):
            doc_type = os.path.splitext(basename(filename))[0]
            self.delete_documents(doc_type)
            self.put_mapping(doc_type)
            call("cd %s && rm *_oc_split*" % exploded_dir, shell=True)
            call("cd %s && split -b 20000000 %s %s" % (
                exploded_dir, doc_type,
                doc_type + '_oc_split'), shell=True)
            for split_file in glob.iglob(os.path.join(exploded_dir, '*_oc_split*')):
                print("Processing %s" % split_file)
                with codecs.open(split_file, 'r', 'utf-8') as f:
                    actions = list({
                                       "_index": OPEN_CRICKET_INDEX,
                                       "_type": doc_type,
                                       "_source": {
                                           "question": line.strip()
                                       }} for line in f)
                    elasticsearch.helpers.bulk(self.es, actions, chunk_size=200000)
                gc.collect()
            call("cd %s && rm *_oc_split*" % exploded_dir, shell=True)

    def delete_index(self):
        self.es.indices.delete(index=OPEN_CRICKET_INDEX)

    def dedup_syntax_list(self, syntax_list):
        deduped_list = []
        for syntax in syntax_list:
            if not self.contains(deduped_list, syntax): deduped_list.append(syntax)
        return deduped_list

    def strip_permutation(self, syntax, possible_filters, upto=1):
        if len(set(syntax.split()).intersection(possible_filters)) <= upto:
            return syntax
        else:
            return None

    def contains(self, syntax_list, syntax):
        for s in syntax_list:
            if Counter(s.split()) == Counter(syntax.split()): return True
        return False
