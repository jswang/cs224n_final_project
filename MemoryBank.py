import random
from typing import Tuple, List

from transformers import AutoTokenizer, AutoModelForSequenceClassification, AutoModelForSeq2SeqLM, logging
import torch
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer

import models
from models import baseline_config
import json
from MemoryEntry import MemoryEntry

# only log errors
logging.set_verbosity_error()


class MemoryBank:
    def __init__(self, config=baseline_config):
        """
        Create a MemoryBank model based on configuration.
        """
        self.alpha = config['flip_alpha']
        self.beta = config['flip_beta']
        self.gamme = config['flip_gamme']
        self.device = config["device"]

        # Sentence tokenizer and NLI model which outputs relation of premise and hypothesis
        self.nli_tokenizer = AutoTokenizer.from_pretrained(config["nli_model"])
        self.nli_model = AutoModelForSequenceClassification.from_pretrained(
            config["nli_model"])
        self.nli_model.to(self.device)

        # Question answering model and tokenizer
        self.qa_tokenizer = AutoTokenizer.from_pretrained(config["qa_model"])
        self.qa_model = AutoModelForSeq2SeqLM.from_pretrained(
            config["qa_model"])
        self.qa_model.to(self.device)

        # Plaintext beliefs: question answer pairs
        self.mem_bank = []

        # Similarity threshold for index lookup
        self.threshold = config["sentence_similarity_threshold"]
        # Maximum number of characters in input sequence
        self.max_length = config["max_input_char_length"]
        # Whether we use the flipping functionality
        self.enable_flip = config["enable_flip"]
        # The type of feedback we will be creating
        self.feedback = config["feedback_type"]
        if self.feedback == "topic":
            self.entities_dict = {k: [] for k in json.load(
                open("data/silver_facts.json")).keys()}
        else:
            self.entities_dict = dict()
        self.n_feedback = 3

        # Model that goes from sentence to sentence representation
        self.sent_model = SentenceTransformer(config["sentence_model"])
        self.sent_model.to(self.device)
        # Embedded sentence index, allows us to look up quickly
        self.index = faiss.IndexFlatIP(
            self.sent_model.get_sentence_embedding_dimension())

    def find_same_topic(self, questions: List[MemoryEntry]) -> List[str]:
        """
        Given a list of questions, return all of the related topic sentences in a single string
        """
        result_topics = []
        for q in questions:
            topics = random.choices(
                self.entities_dict[q.get_entity()], k=self.n_feedback)
            result_topics.append(
                " ".join([t.get_declarative_statement() for t in topics]))
            self.entities_dict[q.get_entity()].append(q)
        return result_topics

    def generate_feedback(self, questions: List[MemoryEntry]) -> List[str]:
        """
        Given a list of questions, retrieve semantically similar sentences for context
        """
        if self.feedback == "relevant":
            s_embed = self.encode_sent(
                [q.get_pos_statement() for q in questions])
            context = []
            for i in range(s_embed.shape[0]):
                retrieved, I = self.retrieve_from_index(
                    s_embed[i, :], feedback_mode=True)
                context.append(" ".join(
                    [r.get_declarative_statement() for r in retrieved[:self.n_feedback]]))
        else:
            # List of strings, each string corresponding to self.n_feedback relevant beliefs
            context = self.find_same_topic(questions)
        # for i in range(len(context)):
        #     print("CONTEXT:", context[i])
        #     print("QUESTION:", questions[i])
        # print(context, "< ======= CONTEXT")
        # print(questions, "< ======== QUESTIONS")
        return context

    def ask_questions(self, questions: List[str], context: List[Tuple[str, str]]) -> List[Tuple[str, float]]:
        """
        Ask the Macaw model a batch of yes or no questions.
        Returns "yes" or "no" and a confidence score
        """
        # Insert feedback if necesasry
        if len(context) == len(questions):
            input_string = [
                f"$answer$ ; $mcoptions$ = (A) yes (B) no; $context$ = {context[i]} ; $question$ = {questions[i]}"
                for i in range(len(context))]
        else:
            input_string = [
                f"$answer$ ; $mcoptions$ = (A) yes (B) no; $question$ = {q}" for q in questions]
        # Tokenize questions
        input_ids = self.qa_tokenizer(
            input_string, padding=True, truncation=True, return_tensors="pt", max_length=self.max_length).input_ids
        input_ids.to(self.device)
        # Ask the questions, include a label to gather confidence
        labels = self.qa_tokenizer(
            "$answer$ = yes", return_tensors="pt", max_length=self.max_length).input_ids
        labels = torch.tile(labels, (len(questions), 1))
        labels.to(self.device)
        # Calculate probability of yes answer
        res = self.qa_model(input_ids, labels=labels)
        res_softmax = torch.softmax(res.logits, dim=2)
        raw_probs = torch.squeeze(torch.gather(
            res_softmax, 2, torch.unsqueeze(labels, 2)))
        output_prob = torch.prod(raw_probs, 1)

        return [("yes", a) if a >= 0.5 else ("no", a) for a in output_prob]

    def add_to_index(self, s_embed: np.array):
        """
        Add sentence embeddings to the index
        """
        s_embed = s_embed.cpu().detach().numpy().astype("float32")
        s_embed /= np.expand_dims(np.linalg.norm(s_embed, axis=-1), 1)
        self.index.add(s_embed)

    def retrieve_from_index(self, sentences: List[MemoryEntry], feedback_mode=False) -> Tuple[List[MemoryEntry], np.array]:
        """
        Retrieve sentence embeddings and sentences from the index.
        s_new is a Tensor, first dimension is batch
        """
        s_embed = self.encode_sent(sentences)
        s_embed = s_embed.cpu().detach().numpy().astype("float32")
        s_embed /= np.expand_dims(np.linalg.norm(s_embed, axis=-1), 1)
        lims, D, I = self.index.range_search(
            x=s_embed, thresh=self.threshold)

        corresponding_indices = [I[lims[i]:lims[i+1]]
                                 for i in range(len(lims) - 1)]
        corresponding_scores = [D[lims[i]:lims[i+1]]
                                for i in range(len(lims) - 1)]

        retrieved = []
        indices = []
        for idx_list, score_list in zip(corresponding_indices, corresponding_scores):
            # Get items in order of most similar
            top_indices = np.argsort(score_list)[::-1]
            # Take top 30 most similar items
            top_indices = top_indices[:30]
            for meta_index in top_indices:
                bank_idx = idx_list[meta_index]
                e = self.mem_bank[bank_idx]
                retrieved.append(e)
                indices.append(bank_idx)

        return retrieved, indices

    def flip_or_keep(self, premises: List[MemoryEntry], premises_indices, hypothesis: MemoryEntry) -> MemoryEntry:
        """
        Decide whether or not to flip the hypothesis given relevant MemoryEntries and their indices.
        """
        if premises == []:
            return hypothesis

        probs = np.array([self.get_relation(p.get_declarative_statement(
        ), hypothesis.get_declarative_statement()) for p in premises])

        n_entail = np.sum(probs.argmax(axis=1) == 0)
        n_contra = np.sum(probs.argmax(axis=1) == 2)

        # if we have more contradictions than we do entailments, we should flip
        # either the hypothesis or one or more premises
        if n_entail < n_contra:
            hypothesis_score = hypothesis.get_confidence()
            premise_scores = np.array([r.get_confidence() for r in premises])

            hypothesis_votes = np.count_nonzero(
                hypothesis_score > premise_scores)
            premise_votes = np.count_nonzero(hypothesis_score < premise_scores)

            # if our QA model is more confident about the hypothesis,
            # the hypothesis is good and we should flip some premises
            if hypothesis_votes > premise_votes:
                # flip premises whose QA scores are lower than hypothesis score
                for idx, r in zip(premises_indices, premises):
                    if r.confidence < hypothesis_score:
                        self.mem_bank[idx].flip()

            # if our QA model is more confident about premises,
            # the hypothesis isn't good and we should flip it
            else:
                hypothesis.flip()

        return hypothesis

    def encode_sent(self, sentences: List[MemoryEntry]):
        return self.sent_model.encode([s.get_pos_statement() for s in sentences], device=self.device, convert_to_tensor=True)

    def add_to_bank(self, new_entries: List[MemoryEntry]):
        """ Usage: add_to_bank('owl', 'HasA,Vertebrate', 'yes')"""

        self.mem_bank += new_entries

        # Embed and add to index
        s_embed = self.encode_sent(new_entries)
        self.add_to_index(s_embed)

    def clear_bank(self):
        """
        Clears all entries from memory bank
        """
        self.mem_bank = []
        self.index.reset()

    def get_relation(self, premise: str, hypothesis: str):
        """
        Given premise and hypothesis, output entailment/neutral/contradiction
        """
        tokenized_input_seq_pair = self.nli_tokenizer.encode_plus(premise, hypothesis,
                                                                  max_length=self.max_length,
                                                                  return_token_type_ids=True, truncation=True)
        input_ids = torch.Tensor(
            tokenized_input_seq_pair['input_ids']).long().unsqueeze(0).to(self.device)
        # remember bart doesn't have 'token_type_ids', remove the line below if you are using bart.
        token_type_ids = torch.Tensor(
            tokenized_input_seq_pair['token_type_ids']).long().unsqueeze(0).to(self.device)
        attention_mask = torch.Tensor(
            tokenized_input_seq_pair['attention_mask']).long().unsqueeze(0).to(self.device)
        outputs = self.nli_model(input_ids,
                                 attention_mask=attention_mask,
                                 token_type_ids=token_type_ids,
                                 labels=None)
        predicted_probability = torch.softmax(outputs[0], dim=1)[
            0].tolist()  # batch_size only one
        return predicted_probability

    def forward(self, inputs: List[Tuple[str, str, str]]):
        """
        Forward pass of the model on a batch of triplets
        Arguments:
        `inputs` - batch of inputs to parse, expect of tuple of (entity, relation, answer)
        """
        # triplet to question
        questions = [MemoryEntry(i[0], i[1]) for i in inputs]
        context = []
        # Generate context if necessary
        if self.feedback is not None:
            context = self.generate_feedback(questions)
        # Ask your question
        answers = self.ask_questions(
            [q.get_question() for q in questions], context)
        for (i, (ans, conf)) in enumerate(answers):
            questions[i].set_answer(ans)
            questions[i].set_confidence(conf.item())
        statements = questions

        # Check against existing constraints to flip as necessary
        if self.enable_flip:
            retrieved, inds = self.retrieve_from_index(statements)
            statements = [self.flip_or_keep(
                retrieved, inds, s) for s in statements]

        # Add flipped statements to the bank.
        self.add_to_bank(statements)

        # Return the answers for this batch
        return answers
