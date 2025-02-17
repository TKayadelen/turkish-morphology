# coding=utf-8
# Copyright 2019 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tool to compile morphotactics FST into OpenFST format."""

import argparse
import collections
import glob
import io
import logging
import os
import re

from src.analyzer.lexicon import parser as lexicon_parser
from src.analyzer.lexicon import reader as lexicon_reader
from src.analyzer.lexicon import validator as lexicon_validator
from src.analyzer.morphotactics import common
from src.analyzer.morphotactics import parser as morphotactics_parser
from src.analyzer.morphotactics import reader as morphotactics_reader
from src.analyzer.morphotactics import rule_pb2
from src.analyzer.morphotactics import validator as morphotactics_validator


def _check_if_directory_exists(path):
  """Raises an exception if 'path' is not a path to an existing directory."""
  if not os.path.isdir(path):
    raise argparse.ArgumentTypeError(
        f"Not a valid path to an existing directory: '{path}'")
  return path


_ARG_PARSER = argparse.ArgumentParser(
    description="""This tool takes a set of lexicon and morphotactics source
    files and outputs AT&T FSM format symbols table and text FST files, which
    are used in building morphotactics FST (second layer of analysis) using
    OpenFst.

    Output symbols table will not suffice to build the morphotactics FST. It
    will only contain labels for the complex symbols (such as meta-morphemes
    that are used on the output side of rewrite rules, or morphological anlysis
    tags that are used on the input side of the rewrite rules of the
    morphotactics model). Thus, it should first be concatenated with the base
    symbols table (which contains the labels for the characters of the input
    words) before building the morphotactics FST using OpenFST. For further
    details, see the comments in //src/analyzer/build.sh.

    In case of an input illformed lexicon entry annotation, or morphotactics
    rewrite rule definition, the tool raises MorphotacticsCompilerError.

    Note that all the files under the lexicon_dir that have the ".tsv" file
    extension, and those under the morphotactics_dir that have ".txt" file
    extension will be picked up by this tool throughout compilation (so abstain
    from having redundant files with those file extensions in respective
    directories, or probably the tool will raise MorphotacticsCompilerError
    to signal illformed source data).

    Generated symbols table and text FST files will respectively have the names
    'complex_symbols.syms' and 'morphotactics.txt'. They will be written under
    output_dir.""")
_ARG_PARSER.add_argument(
    "--lexicon_dir",
    default="src/analyzer/lexicon/base",
    type=_check_if_directory_exists,
    help="""path to the directory that contains the source lexicon annotation
    TSV dumps.""")
_ARG_PARSER.add_argument(
    "--morphotactics_dir",
    default="src/analyzer/morphotactics/model",
    type=_check_if_directory_exists,
    help="""path to the directory that contains the source text files that
    define rewrite rules of morphotactics model.""")
_ARG_PARSER.add_argument(
    "--output_dir",
    default="bin",
    type=str,
    help="""path to the directory to which compiled OpenFST format transducer
    specification and symbols table file will be written to as text file""")


class MorphotacticsCompilerError(Exception):
  """Raised when one of the source files contains an illformed line or entry."""


_SYMBOLS_REGEX = re.compile(
    # First inflectional group.
    r"\(.+?\[[A-Z\.,:\(\)\'\-\"`\$]+?\]|"
    # Inflectional group boundaries.
    r"\)\(\[[A-Z]+?\]|"
    # Derivational morphemes.
    r"-(?:[^\W\d_]|')+?\[[A-z]+?=[A-z]+?\]|"
    # Inflectional morphemes and features.
    r"\+(?:[^\W\d_]|['\.])*?\[[A-z]+?=[A-z0-9]+?\]|"
    # Proper noun analysis.
    r"\)\+\[Proper=(?:True|False)\]|"
    # Numbers.
    r"\d+(?:\[[A-Z]+?\])?|"
    # Parenthesis or decimal point separators.
    r"[\(\.,]")


def _get_lexicon_rules(lexicon_dir):
  """Parses lexicon into valid rewrite rules.

  Args:
    lexicon_dir: str, path to the directory that contains the source lexicon
        annotation TSV dumps. All files that have the ".tsv" file extension
        under this directory will be picked up by this function and will
        attempted to be parsed into a set of rewrite rule objects.

  Raises:
    MorphotacticsCompilerError: one of the lexicon entry annotations in the
        lexicon source files is illformed, or no valid rewrite rules can be
        generated from the annotation in lexicon source files.

  Returns:
    rule_pb2.RewriteRuleSet, set of validated and parsed lexicon rewrite
    rule objects.
  """
  def _read_rule_set(path):
    logging.info("reading rewrite rules from %r", path)
    entries = lexicon_reader.read_lexicon_source(path)  # might throw IOError.

    for index, entry in entries.items():
      try:
        lexicon_validator.validate(entry)
      except lexicon_validator.InvalidLexiconEntryError as error:
        raise MorphotacticsCompilerError(
            f"Lexicon entry at line {index} of '{path}' is illformed. {error}")

    return lexicon_parser.parse(list(entries.values()))

  paths = sorted(glob.glob(f"{lexicon_dir}/*.tsv"))
  rule_sets = [_read_rule_set(p) for p in paths]
  lexicon = rule_pb2.RewriteRuleSet()
  lexicon.rule.extend(r for rs in rule_sets for r in rs.rule)

  if not lexicon.rule:
    raise MorphotacticsCompilerError("no valid lexicon rewrite rules found.")

  return lexicon


def _get_morphotactics_rules(morphotactics_dir):
  """Parses morphotactics into valid rewrite rules.

  Args:
    morphotactics_dir: str, path to the directory that contains the source text
        files that define rewrite rules of morphotactics FST. All files that
        have the ".txt" file extension under this directory will be picked up
        by this function and will attempted to be parsed into a set of rewrite
        rule objects.

  Raises:
    MorphotacticsCompilerError: one of the morphotactics rewrite rule
        definitions in the morphotactics source files is illformed, or no valid
        rewrite rules can be generated from the definitions in the
        morphotactics source files.

  Returns:
    rule_pb2.RewriteRuleSet, set of validated and parsed morphotactics rewrite
    rule objects.
  """
  def _read_rule_set(path):
    logging.info("reading rewrite rules from %r", path)
    # Below read call might throw IOError.
    lines = morphotactics_reader.read_morphotactics_source(path)

    for index, line in lines.items():
      try:
        morphotactics_validator.validate(line)
      except morphotactics_validator.InvalidMorphotacticsRuleError as error:
        raise MorphotacticsCompilerError(
            f"Rewrite rule at line {index} of '{path}' is illformed. {error}")

    return morphotactics_parser.parse(list(lines.values()))

  paths = sorted(glob.glob(f"{morphotactics_dir}/*.txt"))
  rule_sets = [_read_rule_set(p) for p in paths]
  morphotactics = rule_pb2.RewriteRuleSet()
  morphotactics.rule.extend(r for rs in rule_sets for r in rs.rule)

  if not morphotactics.rule:
    raise MorphotacticsCompilerError(
        "no valid morphotactics rewrite rules found.")

  return morphotactics


def _remove_duplicate_rules(rule_set):
  """Removes duplicate rewrite rules objects that are in the rule set.

  This function preserves the order of the rewrite rules in the rule set and
  does de-duplication in-place by just keeping the last occurrence of a
  duplicate rule.

  Args:
    rule_set: rule_pb2.RewriteRuleSet, array of rewrite rule objects that
        defines the state transition arcs of the morphocatics FST.
  """
  def _key_and_value(rule):
    return (rule.from_state, rule.to_state, rule.input, rule.output), rule

  inverted = collections.OrderedDict(map(_key_and_value, rule_set.rule))
  duplicates = len(rule_set.rule) - len(inverted)

  if duplicates:
    logging.info("found %i duplicate rewrite rules, removing them", duplicates)
    rule_set.ClearField("rule")
    rule_set.rule.extend([r for r in inverted.values()])


def _symbols_of_input(label):
  """Extracts FST symbols that compose complex input label of the rewrite rule.

  FST symbols of a complex input label is;
    - Epsilon symbol if the complex input label is an epsilon symbol
      (e.g. ['<eps>'] for label '<eps>').
    - Digits of the complex input label if it is only composed of digits
      without any feature analysis tags (e.g. ['9', '0'] for the label '90').
    - Tokenized inflectional group boundaries, inflectional or derivational
      morphemes, proper noun and feature analyses tags, numbers, and punction
      if the complex input label is composed of these units (e.g. [')([VN]',
      '-YAn[Derivation=PresNom]'] for the label
      ')([VN]-YAn[Derivation=PresNom]').

  Args:
    label: str, complex input label of a morphotactics FST rewrite rule.

  Returns:
    list of str, each item is a FST symbol that is used in the complex input
    label of the rewrite rule. For labels that do not represent epsilon, FST
    symbols are returned in the same order as they appear in the complex input
    label, and duplicate symbols are preserved.
  """
  if label == common.EPSILON:
    return [label]

  # We add a state transition arc for each digit of a multi-digit number.
  if "[" not in label:
    return list(label)

  # We add a state transition arc for each inflectional or derivational
  # morpheme, inflectional group boundary, and proper noun analysis tag.
  return _SYMBOLS_REGEX.findall(label)


def _symbols_of_output(label):
  """Extracts FST symbols that compose complex output label of the rewrite rule.

  FST symbols of a complex output label is;
    - Epsilon symbol if the complex output label is an epsilon symbol
      (e.g. ['<eps>'] for the label '<eps>').
    - All characters of the complex output label if it is not an epsilon symbol
      (e.g. ['{', 'l', 'p'] for the label '{lp').

  Args:
    label: str, complex output label of a morphotactics FST rewrite rule.

  Returns:
    list of str, each item is a FST symbol that is used in the complex output
    label of the rewrite rule. For labels that do not represent epsilon, FST
    symbols are returned in the same order as they appear in the complex output
    label, and duplicate symbols are preserved.
  """
  if label == common.EPSILON:
    return [label]

  # We add a new state transition arc for each character of the output token.
  return list(label)


def _symbols_table_file_content(rule_set):
  r"""Generates the content of the complex symbols table file.

  Generated file is in AT&T format. It defines the labels for state transition
  arcs and assigns a unique index to each. The first label in the file get
  the index 983040 (decimal value for the beginning of the Unicode private use
  area). Successive labels have incremental index.

  Note that we do not generate distinct symbols table files for complex input
  and output labels, yet only create a single symbols table file that contains
  the union of the set of labels on both sides.

  Args:
    rule_set: rule_pb2.RewriteRuleSet, array of rewrite rule objects that
        defines the state transition arcs of the morphocatics FST.

  Yields:
    Str, lines of symbols table file, where each defines an FST symbol in
    the form of 'SYMBOL INDEX\n' (e.g. '(abanoz[NN]	983041\n').
  """
  def _line(symbol, index):
    return f"{symbol}\t{index}\n".encode("utf-8")

  fst_symbols = []

  for rule in rule_set.rule:
    fst_symbols.extend(_symbols_of_input(rule.input))
    fst_symbols.extend(_symbols_of_output(rule.output))

  unique_symbols = set(fst_symbols).difference({common.EPSILON})
  complex_symbols = [s for s in unique_symbols if len(s) > 1]

  index = 983040  # start of the Unicode private use area.

  for symbol in sorted(complex_symbols):
    yield _line(symbol, index)
    index += 1

  logging.info("generated complex symbols file content")


def _text_fst_file_content(rule_set):
  r"""Generates the content of the text FST file.

  Generated file is in AT&T format. It defines the state transition arcs and
  input/output label pairs of the morphotactics model.

  Args:
    rule_set: rule_pb2.RewriteRuleSet, array of rewrite rule objects that
        defines the state transition arcs of the morphocatics FST.

  Yields:
    Str, lines of text FST file, where each defines a state transition arc
    in the form of 'FROM_INDEX TO_INDEX INPUT OUTPUT\n'
    (e.g. '0 5771 (dokun[VB] d\n').
  """

  class _Local(object):
    state_count = 0

  def _new_state_index():
    _Local.state_count += 1
    return _Local.state_count

  def arc(from_, to, input_=common.EPSILON, output=common.EPSILON):
    return f"{from_}\t{to}\t{input_}\t{output}\n".encode("utf-8")

  start_state = common.START_STATE
  epsilon = common.EPSILON

  index_of = collections.defaultdict(_new_state_index)
  index_of[start_state] = 0

  for rule in rule_set.rule:
    input_symbols = _symbols_of_input(rule.input)
    output_symbols = _symbols_of_output(rule.output)

    # Pad list of input and output symbols with epsilon transitions until they
    # have the same length.
    while len(input_symbols) < len(output_symbols):
      input_symbols.append(epsilon)

    while len(output_symbols) < len(input_symbols):
      output_symbols.append(epsilon)

    from_ = index_of[rule.from_state]

    for input_, output in zip(input_symbols, output_symbols):
      to = _new_state_index()
      yield arc(from_, to, input_, output)
      from_ = to

    yield arc(from_, index_of[rule.to_state])

  # Last line should be the index of the accept state.
  yield f"{index_of[common.ACCEPT_STATE]}\n".encode("utf-8")
  logging.info("generated text FST file content")


def _make_output_directory(output_dir):
  """Makes the output directory if it does not exist."""
  if output_dir and not os.path.exists(output_dir):
    os.makedirs(output_dir)
    logging.info("output directory does not exist, made %r", output_dir)


def _write_file(output_path, file_content):
  r"""Writes the file content to the output path as a text file.

  Args:
    output_path: str, path to the file to which file content will be written as
        text.
    file_content: list of unicode, each item contains a line of the file
        content which will be written to the 'output_path'. It is assumed that
        each item ends with a '\n'.

  Raises:
    IOError: file content could not be written to the 'output_path'.
  """
  with io.open(output_path, "w+", encoding="utf-8") as f:
    for line in file_content:
      f.write(line.decode("utf-8"))

  logging.info("wrote to %r", output_path)


def main(args):
  # Below rewrite rule retrieval calls might throw IOError or
  # MorphotacticsCompilerError.
  lexicon = _get_lexicon_rules(args.lexicon_dir)
  morphotactics = _get_morphotactics_rules(args.morphotactics_dir)

  merged = rule_pb2.RewriteRuleSet()
  merged.rule.extend(lexicon.rule)
  merged.rule.extend(morphotactics.rule)
  _remove_duplicate_rules(merged)

  symbols_content = _symbols_table_file_content(merged)
  fst_content = _text_fst_file_content(merged)

  _make_output_directory(args.output_dir)
  symbols_path = os.path.join(args.output_dir, "complex_symbols.syms")
  _write_file(symbols_path, symbols_content)
  fst_path = os.path.join(args.output_dir, "morphotactics.txt")
  _write_file(fst_path, fst_content)


if __name__ == "__main__":
  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s %(levelname)s: %(message)s",
      datefmt="%H:%M:%S")
  main(_ARG_PARSER.parse_args())
