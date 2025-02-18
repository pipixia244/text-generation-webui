import ast
import copy
import html
import random
import re
import time
import traceback

import numpy as np
import torch
import transformers
from transformers import LogitsProcessorList, GenerationConfig

def is_torch_xpu_available():
    return False

import modules.shared as shared
from modules.callbacks import (
    Iteratorize,
    Stream,
    _StopEverythingStoppingCriteria
)
from modules.extensions import apply_extensions
from modules.grammar import GrammarLogitsProcessor
from modules.html_generator import generate_4chan_html, generate_basic_html
from modules.logging_colors import logger
from modules.models import clear_torch_cache, local_rank

generation_config_default = GenerationConfig(
    temperature=0.1,
    top_p=0.3,
    top_k=40,
    repetition_penalty=1,
    max_new_token=1024,
    do_sample=True,
    num_beams=1
)

def generate_reply(*args, **kwargs):
    shared.generation_lock.acquire()
    try:
        for result in _generate_reply(*args, **kwargs):
            yield result
    finally:
        shared.generation_lock.release()


def generate_reply_token(*args, **kwargs):
    shared.generation_lock.acquire()
    try:
        for result, new_token in _generate_reply(is_return_token_cnt=True, *args, **kwargs):
            yield result, new_token
    except Exception as e:
        print(e)
    finally:
        shared.generation_lock.release()

def _generate_reply(question, state, stopping_strings=None, is_chat=False, escape_html=False, is_return_token_cnt=False, generation_config=generation_config_default):

    state_tmp = state

    # Find the appropriate generation function
    generate_func = apply_extensions('custom_generate_reply')
    if generate_func is None:
        if shared.model_name == 'None' or shared.model is None:
            logger.error("No model is loaded! Select one in the Model tab.")
            yield ''
            return

        if shared.model.__class__.__name__ in ['LlamaCppModel', 'RWKVModel', 'ExllamaModel', 'Exllamav2Model', 'CtransformersModel']:
            generate_func = generate_reply_custom
        else:
            generate_func = generate_reply_HF

    # Prepare the input
    original_question = question
    if not is_chat:
        state = apply_extensions('state', state)
        question = apply_extensions('input', question, state)

    # Find the stopping strings
    all_stop_strings = []
    # for st in (stopping_strings, ast.literal_eval(f"[{state['custom_stopping_strings']}]")):
    #     if type(st) is list and len(st) > 0:
    #         all_stop_strings += st

    if shared.args.verbose:
        print(f'\n\n{question}\n--------------------\n')

    shared.stop_everything = False
    clear_torch_cache()
    seed = set_manual_seed(state['seed'])
    last_update = -1
    reply = ''
    new_token = 0
    is_stream = state['stream']
    if len(all_stop_strings) > 0 and not state['stream']:
        state = copy.deepcopy(state)
        state['stream'] = True

    # Generate
    state['max_tokens_second'] = 0
    if is_return_token_cnt:
        for reply, token_cnt in generate_func(question, original_question, seed, state_tmp, stopping_strings, is_chat=is_chat, is_return_token_cnt=is_return_token_cnt):
            if escape_html:
                reply = html.escape(reply)

            reply, stop_found = apply_stopping_strings(reply, all_stop_strings)
            new_token = token_cnt
            if is_stream:
                cur_time = time.time()

                # Maximum number of tokens/second
                if state['max_tokens_second'] > 0:
                    diff = 1 / state['max_tokens_second'] - (cur_time - last_update)
                    if diff > 0:
                        time.sleep(diff)

                    last_update = time.time()
                    yield reply, token_cnt

                # Limit updates to 24 per second to not stress low latency networks
                else:
                    if cur_time - last_update > 0.041666666666666664:
                        last_update = cur_time
                        yield reply, token_cnt

            if stop_found or (state['max_tokens_second'] > 0 and shared.stop_everything):
                break
            
    else:
        for reply in generate_func(question, original_question, seed, state, stopping_strings, is_chat=is_chat, is_return_token_cnt=is_return_token_cnt):
            if escape_html:
                reply = html.escape(reply)

            reply, stop_found = apply_stopping_strings(reply, all_stop_strings)
            if is_stream:
                cur_time = time.time()

                # Maximum number of tokens/second
                if state['max_tokens_second'] > 0:
                    diff = 1 / state['max_tokens_second'] - (cur_time - last_update)
                    if diff > 0:
                        time.sleep(diff)

                    last_update = time.time()
                    yield reply

                # Limit updates to 24 per second to not stress low latency networks
                else:
                    if cur_time - last_update > 0.041666666666666664:
                        last_update = cur_time
                        yield reply

            if stop_found or (state['max_tokens_second'] > 0 and shared.stop_everything):
                break

    if not is_chat:
        reply = apply_extensions('output', reply, state)
    if is_return_token_cnt:
        yield reply, new_token
    else:
        yield reply


def encode(prompt, add_special_tokens=True, add_bos_token=True, truncation_length=None):
    if shared.tokenizer is None:
        raise ValueError('No tokenizer is loaded')

    if shared.model.__class__.__name__ in ['LlamaCppModel', 'RWKVModel', 'CtransformersModel', 'Exllamav2Model']:
        input_ids = shared.tokenizer.encode(str(prompt))
        if shared.model.__class__.__name__ not in ['Exllamav2Model']:
            input_ids = np.array(input_ids).reshape(1, len(input_ids))
    else:
        input_ids = shared.tokenizer.encode(str(prompt), return_tensors='pt', add_special_tokens=add_special_tokens)

        # This is a hack for making replies more creative.
        if not add_bos_token and input_ids[0][0] == shared.tokenizer.bos_token_id:
            input_ids = input_ids[:, 1:]

    # Handling truncation
    if truncation_length is not None:
        input_ids = input_ids[:, -truncation_length:]

    if shared.model.__class__.__name__ in ['LlamaCppModel', 'RWKVModel', 'ExllamaModel', 'Exllamav2Model', 'CtransformersModel'] or shared.args.cpu:
        return input_ids
    elif shared.args.deepspeed:
        return input_ids.to(device=local_rank)
    elif torch.backends.mps.is_available():
        device = torch.device('mps')
        return input_ids.to(device)
    elif is_torch_xpu_available():
        return input_ids.to("xpu:0")
    else:
        return input_ids.cuda()


def decode(output_ids, skip_special_tokens=True):
    if shared.tokenizer is None:
        raise ValueError('No tokenizer is loaded')

    return shared.tokenizer.decode(output_ids, skip_special_tokens)


def get_encoded_length(prompt):
    length_after_extensions = apply_extensions('tokenized_length', prompt)
    if length_after_extensions is not None:
        return length_after_extensions

    return len(encode(prompt)[0])


def get_token_ids(prompt):
    tokens = encode(prompt)[0]
    decoded_tokens = [shared.tokenizer.decode([i]) for i in tokens]

    output = ''
    for row in list(zip(tokens, decoded_tokens)):
        output += f"{str(int(row[0])).ljust(5)}  -  {repr(row[1])}\n"

    return output


def get_max_prompt_length(state):
    return state['truncation_length'] - state['max_new_tokens']


def generate_reply_wrapper(question, state, stopping_strings=None):
    """
    Returns formatted outputs for the UI
    """
    reply = question if not shared.is_seq2seq else ''
    yield formatted_outputs(reply, shared.model_name)

    for reply in generate_reply(question, state, stopping_strings, is_chat=False, escape_html=True):
        if not shared.is_seq2seq:
            reply = question + reply

        yield formatted_outputs(reply, shared.model_name)


def formatted_outputs(reply, model_name):
    if any(s in model_name for s in ['gpt-4chan', 'gpt4chan']):
        reply = fix_gpt4chan(reply)
        return html.unescape(reply), generate_4chan_html(reply)
    else:
        return html.unescape(reply), generate_basic_html(reply)


def fix_gpt4chan(s):
    """
    Removes empty replies from gpt4chan outputs
    """
    for i in range(10):
        s = re.sub("--- [0-9]*\n>>[0-9]*\n---", "---", s)
        s = re.sub("--- [0-9]*\n *\n---", "---", s)
        s = re.sub("--- [0-9]*\n\n\n---", "---", s)

    return s


def fix_galactica(s):
    """
    Fix the LaTeX equations in GALACTICA
    """
    s = s.replace(r'\[', r'$')
    s = s.replace(r'\]', r'$')
    s = s.replace(r'\(', r'$')
    s = s.replace(r'\)', r'$')
    s = s.replace(r'$$', r'$')
    s = re.sub(r'\n', r'\n\n', s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


def get_reply_from_output_ids(output_ids, input_ids, original_question, state, is_chat=False, is_return_token_cnt=False):
    state['skip_special_tokens'] = True
    if shared.is_seq2seq:
        new_tokens = len(output_ids)
        reply = decode(output_ids, state['skip_special_tokens'])
    else:
        new_tokens = len(output_ids) - len(input_ids[0])
        reply = decode(output_ids[-new_tokens:], state['skip_special_tokens'])
        # Prevent LlamaTokenizer from skipping a space
        if type(shared.tokenizer) in [transformers.LlamaTokenizer, transformers.LlamaTokenizerFast] and len(output_ids) > 0:
            if shared.tokenizer.convert_ids_to_tokens(int(output_ids[-new_tokens])).startswith('▁'):
                reply = ' ' + reply
    if is_return_token_cnt:
        return reply, new_tokens
    else:
        return reply


def set_manual_seed(seed):
    seed = int(seed)
    if seed == -1:
        seed = random.randint(1, 2**31)

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    elif is_torch_xpu_available():
        torch.xpu.manual_seed_all(seed)
    return seed


def stop_everything_event():
    shared.stop_everything = True


def apply_stopping_strings(reply, all_stop_strings):
    stop_found = False
    for string in all_stop_strings:
        idx = reply.find(string)
        if idx != -1:
            reply = reply[:idx]
            stop_found = True
            break

    if not stop_found:
        # If something like "\nYo" is generated just before "\nYou:"
        # is completed, trim it
        for string in all_stop_strings:
            for j in range(len(string) - 1, 0, -1):
                if reply[-j:] == string[:j]:
                    reply = reply[:-j]
                    break
            else:
                continue

            break

    return reply, stop_found


def generate_reply_HF(question, original_question, seed, state, stopping_strings=None, is_chat=False, is_return_token_cnt=False):
    
    question_tmp = question
    state_tmp = state
    state['stream'] = False
    generation_config = GenerationConfig.from_dict(state)
    # generation_config = GenerationConfig(
    #     temperature=state['temperature'],
    #     top_p = state['top_p'],
    #     top_k = state['top_k'],
    #     max_new_tokens = state['max_new_tokens'],
    #     do_sample = state['do_sample'],
    #     # repetition_penalty = state['repetition_penalty'],
    #     frequency_penalty = 1.1,
    #     num_beams = state['num_beams'],
    #     penalty_alpha = state['penalty_alpha']
    # )
    
    generate_params = {}
    # for k in ['max_new_tokens', 'do_sample', 'temperature', 'top_p', 'typical_p', 'repetition_penalty', 'presence_penalty', 'frequency_penalty', 'repetition_penalty_range', 'encoder_repetition_penalty', 'top_k', 'min_length', 'no_repeat_ngram_size', 'num_beams', 'penalty_alpha', 'length_penalty', 'early_stopping', 'tfs', 'top_a', 'mirostat_mode', 'mirostat_tau', 'mirostat_eta', 'guidance_scale']:
    #     generate_params[k] = state[k]

    # if state['negative_prompt'] != '':
    #     generate_params['negative_prompt_ids'] = encode(state['negative_prompt'])

    # for k in ['epsilon_cutoff', 'eta_cutoff']:
    #     if state[k] > 0:
    #         generate_params[k] = state[k] * 1e-4

    # if state['ban_eos_token']:
    #     generate_params['suppress_tokens'] = [shared.tokenizer.eos_token_id]

    # if state['custom_token_bans']:
    #     to_ban = [int(x) for x in state['custom_token_bans'].split(',')]
    #     if len(to_ban) > 0:
    #         if generate_params.get('suppress_tokens', None):
    #             generate_params['suppress_tokens'] += to_ban
    #         else:
    #             generate_params['suppress_tokens'] = to_ban

    # generate_params.update({'use_cache': not shared.args.no_cache})
    # if shared.args.deepspeed:
    #     generate_params.update({'synced_gpus': True})

    # Encode the input
    input_ids = encode(question)
    output = input_ids[0]
    cuda = not any((shared.args.cpu, shared.args.deepspeed))

    # Add the encoded tokens to generate_params
    question, input_ids, inputs_embeds = apply_extensions('tokenizer', state, question, input_ids, None)
    original_input_ids = input_ids
    generate_params.update({'inputs': input_ids})
    if inputs_embeds is not None:
        generate_params.update({'inputs_embeds': inputs_embeds})

    # Stopping criteria / eos token
    eos_token_ids = [shared.tokenizer.eos_token_id] if shared.tokenizer.eos_token_id is not None else []
    # generate_params['eos_token_id'] = eos_token_ids
    # generate_params['stopping_criteria'] = transformers.StoppingCriteriaList()
    # generate_params['stopping_criteria'].append(_StopEverythingStoppingCriteria())

    # processor = state.get('logits_processor', LogitsProcessorList([]))
    # # In case a processor is passed by itself.
    # if not isinstance(processor, LogitsProcessorList):
    #     processor = LogitsProcessorList([processor])
    # processor.append(GrammarLogitsProcessor(state['grammar_string']))
    # apply_extensions('logits_processor', processor, input_ids)
    # generate_params['logits_processor'] = processor

    t0 = time.time()
    try:
        if not is_chat and not shared.is_seq2seq:
            if is_return_token_cnt:
                yield '', 0
            else:
                yield ''

        # Generate the entire reply at once.
        if not state['stream']:
            with torch.no_grad():
                print(question_tmp)
                print(generation_config)
                output = shared.model.generate(**shared.tokenizer(question_tmp, return_tensors="pt").to(shared.model.device), generation_config=generation_config)[0]
                # output = shared.model.generate(**generate_params)[0]
                if cuda:
                    output = output.cuda()

            yield get_reply_from_output_ids(output, input_ids, original_question, state_tmp, is_chat=is_chat, is_return_token_cnt=is_return_token_cnt)

        # Stream the reply 1 token at a time.
        # This is based on the trick of using 'stopping_criteria' to create an iterator.
        else:

            def generate_with_callback(callback=None, *args, **kwargs):
                kwargs['stopping_criteria'].append(Stream(callback_func=callback))
                clear_torch_cache()
                with torch.no_grad():
                    shared.model.generate(**kwargs)

            def generate_with_streaming(**kwargs):
                return Iteratorize(generate_with_callback, [], kwargs, callback=None)

            with generate_with_streaming(**generate_params) as generator:
                for output in generator:
                    if output[-1] in eos_token_ids:
                        break

                    yield get_reply_from_output_ids(output, input_ids, original_question, state, is_chat=is_chat, is_return_token_cnt=is_return_token_cnt)

    except Exception:
        traceback.print_exc()
    finally:
        t1 = time.time()
        original_tokens = len(original_input_ids[0])
        new_tokens = len(output) - (original_tokens if not shared.is_seq2seq else 0)
        print(f'Output generated in {(t1-t0):.2f} seconds ({new_tokens/(t1-t0):.2f} tokens/s, {new_tokens} tokens, context {original_tokens}, {(len(question_tmp) - 42)/(t1-t0)} ja charators/s, seed {seed})')


def generate_reply_custom(question, original_question, seed, state, stopping_strings=None, is_chat=False):
    """
    For models that do not use the transformers library for sampling
    """
    seed = set_manual_seed(state['seed'])

    t0 = time.time()
    reply = ''
    try:
        if not is_chat:
            yield ''

        if not state['stream']:
            reply = shared.model.generate(question, state)
            yield reply
        else:
            for reply in shared.model.generate_with_streaming(question, state):
                yield reply

    except Exception:
        traceback.print_exc()
    finally:
        t1 = time.time()
        original_tokens = len(encode(original_question)[0])
        new_tokens = len(encode(original_question + reply)[0]) - original_tokens
        print(f'Output generated in {(t1-t0):.2f} seconds ({new_tokens/(t1-t0):.2f} tokens/s, {new_tokens} tokens, context {original_tokens}, seed {seed})')
        return
