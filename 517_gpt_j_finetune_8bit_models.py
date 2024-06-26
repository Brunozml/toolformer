# -*- coding: utf-8 -*-
"""517 GPT-J / finetune-8bit-models.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1gNgD8SczOyFbR8lQDFx-49u6aEucLCGg
"""

!pip install transformers -q
!pip install bitsandbytes-cuda111==0.26.0 -q
!pip install datasets -q
!pip install accelerate -q
!pip install numpy -q

!pip install bitsandbytes

"""### Fine-tuning 6-Billion GPT-J (& other models) in colab with LoRA and 8-bit compression

This notebook is a simple example for fine-tuning [GPT-J-6B](https://huggingface.co/EleutherAI/gpt-j-6B) with limited memory. A detailed explanation of how it works can be found in [this model card](https://huggingface.co/hivemind/gpt-j-6B-8bit). It is heavily based on [this Colab](https://colab.research.google.com/drive/1ft6wQU0BhqG5PRlwgaZJv2VukKKjU4Es#scrollTo=vfdLQHOuEU7h). Huge thanks to Hivemind!

You can also finetune [GPT-Neo-2.7B](https://huggingface.co/gustavecortal/gpt-neo-2.7B-8bit), [French GPT-J (Cedille's Boris)](https://huggingface.co/gustavecortal/fr-boris-8bit) and [T0-3B](https://huggingface.co/gustavecortal/T0_3B-8bit) with limited memory.

Twitter: [@gustavecortal](https://twitter.com/gustavecortal)
"""

from sklearn.model_selection import train_test_split

import transformers

import pandas as pd

import torch
import torch.nn.functional as F
import numpy as np
from torch import nn
from torch.cuda.amp import custom_fwd, custom_bwd

from bitsandbytes.functional import quantize_blockwise, dequantize_blockwise

from tqdm.auto import tqdm

from google.colab import drive
drive.mount('/content/drive')

"""## Converting the model to 8 bits"""

class FrozenBNBLinear(nn.Module):
    def __init__(self, weight, absmax, code, bias=None):
        assert isinstance(bias, nn.Parameter) or bias is None
        super().__init__()
        self.out_features, self.in_features = weight.shape
        self.register_buffer("weight", weight.requires_grad_(False))
        self.register_buffer("absmax", absmax.requires_grad_(False))
        self.register_buffer("code", code.requires_grad_(False))
        self.adapter = None
        self.bias = bias

    def forward(self, input):
        output = DequantizeAndLinear.apply(input, self.weight, self.absmax, self.code, self.bias)
        if self.adapter:
            clone_output = output + self.adapter(input)
        return clone_output

    @classmethod
    def from_linear(cls, linear: nn.Linear) -> "FrozenBNBLinear":
        weights_int8, state = quantize_blockise_lowmemory(linear.weight)
        return cls(weights_int8, *state, linear.bias)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.in_features}, {self.out_features})"


class DequantizeAndLinear(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(ctx, input: torch.Tensor, weights_quantized: torch.ByteTensor,
                absmax: torch.FloatTensor, code: torch.FloatTensor, bias: torch.FloatTensor):
        weights_deq = dequantize_blockwise(weights_quantized, absmax=absmax, code=code)
        ctx.save_for_backward(input, weights_quantized, absmax, code)
        ctx._has_bias = bias is not None
        return F.linear(input, weights_deq, bias)

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_output: torch.Tensor):
        assert not ctx.needs_input_grad[1] and not ctx.needs_input_grad[2] and not ctx.needs_input_grad[3]
        input, weights_quantized, absmax, code = ctx.saved_tensors
        # grad_output: [*batch, out_features]
        weights_deq = dequantize_blockwise(weights_quantized, absmax=absmax, code=code)
        grad_input = grad_output @ weights_deq
        grad_bias = grad_output.flatten(0, -2).sum(dim=0) if ctx._has_bias else None
        return grad_input, None, None, None, grad_bias


class FrozenBNBEmbedding(nn.Module):
    def __init__(self, weight, absmax, code):
        super().__init__()
        self.num_embeddings, self.embedding_dim = weight.shape
        self.register_buffer("weight", weight.requires_grad_(False))
        self.register_buffer("absmax", absmax.requires_grad_(False))
        self.register_buffer("code", code.requires_grad_(False))
        self.adapter = None

    def forward(self, input, **kwargs):
        with torch.no_grad():
            # note: both quantuized weights and input indices are *not* differentiable
            weight_deq = dequantize_blockwise(self.weight, absmax=self.absmax, code=self.code)
            output = F.embedding(input, weight_deq, **kwargs)
        if self.adapter:
            clone_output = output + self.adapter(input)
        return clone_output

    @classmethod
    def from_embedding(cls, embedding: nn.Embedding) -> "FrozenBNBEmbedding":
        weights_int8, state = quantize_blockise_lowmemory(embedding.weight)
        return cls(weights_int8, *state)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.num_embeddings}, {self.embedding_dim})"


def quantize_blockise_lowmemory(matrix: torch.Tensor, chunk_size: int = 2 ** 20):
    assert chunk_size % 4096 == 0
    code = None
    chunks = []
    absmaxes = []
    flat_tensor = matrix.view(-1)
    for i in range((matrix.numel() - 1) // chunk_size + 1):
        input_chunk = flat_tensor[i * chunk_size: (i + 1) * chunk_size].clone()
        quantized_chunk, (absmax_chunk, code) = quantize_blockwise(input_chunk, code=code)
        chunks.append(quantized_chunk)
        absmaxes.append(absmax_chunk)

    matrix_i8 = torch.cat(chunks).reshape_as(matrix)
    absmax = torch.cat(absmaxes)
    return matrix_i8, (absmax, code)


def convert_to_int8(model):
    """Convert linear and embedding modules to 8-bit with optional adapters"""
    for module in list(model.modules()):
        for name, child in module.named_children():
            if isinstance(child, nn.Linear):
                print(name, child)
                setattr(
                    module,
                    name,
                    FrozenBNBLinear(
                        weight=torch.zeros(child.out_features, child.in_features, dtype=torch.uint8),
                        absmax=torch.zeros((child.weight.numel() - 1) // 4096 + 1),
                        code=torch.zeros(256),
                        bias=child.bias,
                    ),
                )
            elif isinstance(child, nn.Embedding):
                setattr(
                    module,
                    name,
                    FrozenBNBEmbedding(
                        weight=torch.zeros(child.num_embeddings, child.embedding_dim, dtype=torch.uint8),
                        absmax=torch.zeros((child.weight.numel() - 1) // 4096 + 1),
                        code=torch.zeros(256),
                    )
                )

"""You have to Monkey-Patch GPT-J before loading:"""

class GPTJBlock(transformers.models.gptj.modeling_gptj.GPTJBlock):
    def __init__(self, config):
        super().__init__(config)

        convert_to_int8(self.attn)
        convert_to_int8(self.mlp)


class GPTJModel(transformers.models.gptj.modeling_gptj.GPTJModel):
    def __init__(self, config):
        super().__init__(config)
        convert_to_int8(self)


class GPTJForCausalLM(transformers.models.gptj.modeling_gptj.GPTJForCausalLM):
    def __init__(self, config):
        super().__init__(config)
        convert_to_int8(self)


transformers.models.gptj.modeling_gptj.GPTJBlock = GPTJBlock

"""If you're using another 8-bit quantized model (e.g. T0-3B), remember to Monkey-Patch the model using convert_to_int8()"""

class T5ForConditionalGeneration(transformers.models.t5.modeling_t5.T5ForConditionalGeneration):
    def __init__(self, config):
        super().__init__(config)
        convert_to_int8(self)

transformers.models.t5.modeling_t5.T5ForConditionalGeneration = T5ForConditionalGeneration

config = transformers.GPTJConfig.from_pretrained("EleutherAI/gpt-j-6B")
tokenizer = transformers.AutoTokenizer.from_pretrained("EleutherAI/gpt-j-6B")

config.pad_token_id = config.eos_token_id
tokenizer.pad_token = tokenizer.eos_token

gpt = GPTJForCausalLM.from_pretrained("hivemind/gpt-j-6B-8bit", low_cpu_mem_usage=True)
#gpt.save_pretrained('/content/drive/MyDrive/nlp/')
#gpt = GPTJForCausalLM.from_pretrained("gustavecortal/fr-boris-8bit", low_cpu_mem_usage=True) French GPT-J Cedille's Boris

#model_dir = '/content/drive/MyDrive/nlp/'
#gpt = GPTJForCausalLM.from_pretrained(model_dir)

"""## LoRA fine-tuning example

You can load my very small dataset composed of philosophical sentences:
"""

# !gdown --id 1Q1WMjny26VHLKb71iTCHIS5zvdm9c-wz

main_folder = '/content/drive/MyDrive/nlp/'
data = pd.read_csv(main_folder+'CombinedAnnotated.csv')
train, test = train_test_split(data, test_size=0.01, random_state=42)
train.to_csv('/content/CombinedAnnotatedTrain.csv', index=False)
test.to_csv('/content/CombinedAnnotatedTest.csv', index=False)

from datasets import load_dataset
dataset = load_dataset('csv', data_files={'train': '/content/CombinedAnnotatedTrain.csv',
                                              'test': '/content/CombinedAnnotatedTest.csv'})

def tokenize_function(examples):
    return tokenizer(examples["sentence"], padding=True, truncation=True, max_length=128)

tokenized_datasets = dataset.map(tokenize_function, batched=True)
tokenized_datasets = tokenized_datasets.remove_columns(["sentence"])
tokenized_datasets.set_format("torch")

from torch.utils.data import DataLoader

full_train_dataset = tokenized_datasets["train"]
train_dataloader = DataLoader(full_train_dataset, shuffle=False, batch_size=8)

"""Add adapters to Embedding/MLP/Attention/LMHead layers"""

def add_adapters(model, adapter_dim=4, p = 0.1):
    assert adapter_dim > 0

    for name, module in model.named_modules():
      if isinstance(module, FrozenBNBLinear):
          if "attn" in name or "mlp" in name or "head" in name:
              print("Adding adapter to", name)
              module.adapter = nn.Sequential(
                nn.Linear(module.in_features, adapter_dim, bias=False),
                nn.Dropout(p=p),
                nn.Linear(adapter_dim, module.out_features, bias=False),
            )
              print("Initializing", name)
              nn.init.zeros_(module.adapter[2].weight)

          else:
              print("Not adding adapter to", name)
      elif isinstance(module, FrozenBNBEmbedding):
          print("Adding adapter to", name)
          module.adapter = nn.Sequential(
                nn.Embedding(module.num_embeddings, adapter_dim),
                nn.Dropout(p=p),
                nn.Linear(adapter_dim, module.embedding_dim, bias=False),
            )
          print("Initializing", name)
          nn.init.zeros_(module.adapter[2].weight)

add_adapters(gpt)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
gpt.to(device)

from bitsandbytes.optim import Adam8bit

gpt.gradient_checkpointing_enable()
optimizer = Adam8bit(gpt.parameters(), lr=1e-5, weight_decay=0.01)

num_epochs = 5
num_training_steps = num_epochs * len(train_dataloader)

lr_scheduler = transformers.get_linear_schedule_with_warmup(
    optimizer, int(num_training_steps*0.1), num_training_steps
)

filepath = '/content/model.pt'

from tqdm.auto import tqdm

scaler = torch.cuda.amp.GradScaler()
progress_bar = tqdm(range(num_training_steps))
gpt.train()
gpt.gradient_checkpointing_enable()
k = 0

for epoch in range(num_epochs):
    for batch in train_dataloader:

        k = k + 1
        if k % 500 == 0:
          print(k)
          state = {'k' : k, 'epoch': num_epochs, 'lr_scheduler': lr_scheduler.state_dict(), 'state_dict': gpt.state_dict(), 'optimizer': optimizer.state_dict()}
          torch.save(state, filepath)

        batch = {k: v.to(device) for k, v in batch.items()}

        optimizer.zero_grad()

        with torch.cuda.amp.autocast():
          out = gpt.forward(**batch,)

          loss = F.cross_entropy(out.logits[:, :-1, :].flatten(0, -2), batch['input_ids'][:, 1:].flatten(),
                                reduction='mean', label_smoothing=0.1)

        print(loss)

        scaler.scale(loss).backward()
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(gpt.parameters(), 1.0)
        scaler.step(optimizer)
        scaler.update()

        lr_scheduler.step()
        progress_bar.update(1)

"""## Text generation example"""

gpt.eval()
with torch.no_grad():
  prompt = tokenizer("Question: Who does patrick stewart play in star trek? Answer:", truncation=True, padding=True, max_length=128, return_tensors='pt')
  prompt = {key: value.to(device) for key, value in prompt.items()}
  out = gpt.generate(**prompt, max_length=256, top_k=100, top_p=0.9, temperature=1.0, do_sample=True, repetition_penalty = 1.2, num_beams=3)
  print(tokenizer.decode(out[0]))

from operator import floordiv, mul, add, sub

def is_decimal(input_string: str) -> bool:
  try:
    float(input_string)
    return True
  except ValueError:
    return False

def Calculator(input_query: str):
  operators = {'+': add, '-': sub, '*': mul, '/': floordiv}
  try:
    if input_query.isdigit():
      return int(input_query)
    elif is_decimal(input_query):
      return float(input_query)
    for c in operators.keys():
      left, operator, right = input_query.partition(c)
      if operator in operators:
        if operator == '/':
          return str(Calculator(left) // Calculator(right)) + " r"+ str(Calculator(left) % Calculator(right))
        return round(operators[operator](Calculator(left), Calculator(right)), 2)
  except TypeError as e:
    print(f"Error processing the input '{input_query}': {e}")
    return None

!pip install openai -q

from openai import OpenAI
client = OpenAI(api_key="YOUR-API-KEY")

def inference(text_input):
  gpt.eval()
  with torch.no_grad():
    prompt = tokenizer(text_input, truncation=True, padding=True, max_length=128, return_tensors='pt')
    prompt = {key: value.to(device) for key, value in prompt.items()}
    output_tokens = gpt.generate(**prompt, max_length=256, top_k=100, top_p=0.9, temperature=1.0, do_sample=True, repetition_penalty = 1.2, num_beams=3)
    out = tokenizer.decode(output_tokens[0])
    print("Original prompt: "+out)
    if "[Calculator(" in out and "[QA(" in out:
      print("Note, this output below uses both calculator and QA. This will likely not give a right answer")

    api_str = "[Calculator("
    api_str_end = ")]"
    idx_start = 0
    idx_end = 0
    checkpoint = 0
    i = 0
    while api_str in out[checkpoint:len(out)] and i < 5:
      #print("we're using calculator")
      idx_start = checkpoint + out[checkpoint:len(out)].find(api_str) + len(api_str)
      idx_end = checkpoint + out[checkpoint:len(out)].find(api_str_end)
      #assert idx_start <= idx_end, "start index shouldn't be larger than end index, start: "+str(idx_start)+" end: "+str(idx_end)+" i: "+str(i)
      if idx_start > idx_end:
        print("WE SKIPPED")
        break
      expr = out[idx_start:idx_end]
      expr = expr.replace(',', '')
      answer = Calculator(expr)
      if answer == None:
        print("WE SKIPPED")
        break
      if is_decimal(answer):
        answer = round(float(answer), 2)
      print("Computed answer was: "+str(answer))
      new_input = out[0:idx_end+len(api_str_end)]+"="+str(answer)
      prompt = tokenizer(new_input, truncation=True, padding=True, max_length=128, return_tensors='pt')
      prompt = {key: value.to(device) for key, value in prompt.items()}
      output_tokens = gpt.generate(**prompt, max_length=256, top_k=100, top_p=0.9, temperature=1.0, do_sample=True, repetition_penalty = 1.2, num_beams=3)
      out = tokenizer.decode(output_tokens[0])
      if idx_end != 0:
        checkpoint = idx_end + len(api_str_end) # would be pos of '=' in '+3)]='
      else:
        checkpoint = idx_end
      #print(out)
      i += 1

    api_str_2 = "[QA("
    api_str_end_2 = ") ->"
    idx_start_2 = 0
    idx_end_2 = 0
    checkpoint_2 = 0
    j = 0

    while api_str_2 in out[checkpoint_2:len(out)] and j < 5:
      #print("we're using chatgpt for Q&A help")
      idx_start_2 = checkpoint_2 + out[checkpoint_2:len(out)].find(api_str_2) + len(api_str_2)
      idx_end_2 = checkpoint_2 + out[checkpoint_2:len(out)].find(api_str_end_2)
      #assert idx_start_2 <= idx_end_2, "start index shouldn't be larger than end index, start: "+str(idx_start_2)+" end: "+str(idx_end_2)+" i: "+str(j)
      if idx_start > idx_end:
        print("WE SKIPPED")
        break
      prompt = out[idx_start_2:idx_end_2]
      prompt = "Please give a concise answer with only few words. " + prompt
      #print(prompt)
      chat_completion = client.chat.completions.create(messages=[{"role": "user", "content": prompt,}], model="gpt-4",)
      answer = chat_completion.choices[0].message.content
      new_input = out[0:idx_end_2+len(api_str_end_2)]+" "+answer+"]"
      second_try_prompt = tokenizer(new_input, truncation=True, padding=True, max_length=512, return_tensors='pt')
      second_try_prompt = {key: value.to(device) for key, value in second_try_prompt.items()}
      output_tokens = gpt.generate(**second_try_prompt, max_length=512, top_k=100, top_p=0.9, temperature=1.0, do_sample=True, repetition_penalty = 1.2, num_beams=3)
      out = tokenizer.decode(output_tokens[0])
      if idx_end_2 != 0:
        checkpoint_2 = idx_end_2 + len(api_str_end_2)
      else:
        checkpoint_2 = idx_end_2
      j += 1

    return out

infered = inference("Question: Who did Trump face in the 2020 election? ")
print(infered)

'''
import re

file_name = 'ASDivBenchmarkNoAnswers.txt'
output_file_name = 'ASDivBenchmarkVanillaAnswers.txt'

with open(file_name, 'r') as file, open(output_file_name, 'w') as outfile:
  for line in file:
    print("Original: " + line)
    gpt.eval()
    with torch.no_grad():
      prompt = tokenizer(line.strip(), truncation=True, padding=True, max_length=256, return_tensors='pt')
      prompt = {key: value.to(device) for key, value in prompt.items()}
      output_tokens = gpt.generate(**prompt, max_length=256, do_sample=True, repetition_penalty = 1.0, num_beams=3)
      generated_line = tokenizer.decode(output_tokens[0])
      print("Generated: " + generated_line)
      outfile.write(generated_line)
'''

'''
file_name = 'NQBenchmarkNoAnswers.txt'
output_file_name = 'NQBenchmarkVanillaAnswers.txt'

with open(file_name, 'r') as file, open(output_file_name, 'w') as outfile:
  for line in file:
    print("Original: " + line)
    gpt.eval()
    with torch.no_grad():
      prompt = tokenizer(line.strip(), truncation=True, padding=True, max_length=128, return_tensors='pt')
      prompt = {key: value.to(device) for key, value in prompt.items()}
      output_tokens = gpt.generate(**prompt, max_length=256, top_k=100, top_p=0.9, temperature=1.0, do_sample=True, repetition_penalty = 1.2, num_beams=3)
      generated_line = tokenizer.decode(output_tokens[0])
      print("Generated: " + generated_line)
      outfile.write(generated_line)
'''

'''
import re

file_name = 'ASDivBenchmarkNoAnswers.txt'
output_file_name = 'ASDivBenchmarkToolformerAnswers.txt'

with open(file_name, 'r') as file, open(output_file_name, 'w') as outfile:
  for line in file:
    #print(line)
    generated_line = inference(line)
    print(generated_line)
    outfile.write(generated_line)
'''

import re

file_name = 'NQBenchmarkNoAnswers.txt'
output_file_name = 'NQBenchmarkToolformerAnswers.txt'

with open(file_name, 'r') as file, open(output_file_name, 'w') as outfile:
  for line in file:
    print("Original: " + line)
    generated_line = inference(line)
    print("Generated: " + generated_line)
    outfile.write(generated_line)

# Code to help ask gpt-4 to finish QA annotations

'''
import re

file_name = 'NQTrainAnnotated.txt'
output_file_name = 'NQTrainAnnotatedFinal.txt'

def replace_pattern(match):
  return f"[QA({match.group(1)}) -> {answer}]"

with open(file_name, 'r') as file, open(output_file_name, 'w') as outfile:
  for line in file:
    matches = re.findall(r'\[QA\((.*?)\)\]', line)
    prompt = "Please give a concise answer with only few words. "
    answer = ""
    for mat in matches:
      #print(mat)
      prompt = prompt + mat
      chat_completion = client.chat.completions.create(messages=[{"role": "user", "content": prompt,}], model="gpt-4",)
      answer = chat_completion.choices[0].message.content

    modified_line = re.sub(r'\[QA\((.*?)\)\]', replace_pattern, line)
    print(modified_line)

    outfile.write(modified_line)
    #print(answer)
'''