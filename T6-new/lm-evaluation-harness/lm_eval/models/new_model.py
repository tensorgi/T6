# For comparison
# GPT2 using SwiGLU and RMSNorm and QK RMSNorm and RoPE

import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from dataclasses import dataclass
import numpy as np
from transformers import PreTrainedModel
from transformers.configuration_utils import PretrainedConfig
from transformers.modeling_utils import PreTrainedModel
class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6, elementwise_affine=True, memory_efficient=False):
        super().__init__()
        self.dim = dim
        self.eps = eps
        self.elementwise_affine = elementwise_affine
        if self.elementwise_affine:
            self.weight = nn.Parameter(torch.ones(dim))
        else:
            self.register_parameter('weight', None)

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        if self.weight is not None:
            output = output * self.weight
        return output

    def extra_repr(self) -> str:
        return f'dim={self.dim}, eps={self.eps}, elementwise_affine={self.elementwise_affine}'

class Rotary(torch.nn.Module):

    def __init__(self, dim, base=10000):
        super().__init__()
        self.inv_freq = 1.0 / (base ** (torch.arange(0, dim, 2).float() / dim))
        self.seq_len_cached = None
        self.cos_cached = None
        self.sin_cached = None

    def forward(self, x):
        seq_len = x.shape[1]
        if seq_len != self.seq_len_cached:
            self.seq_len_cached = seq_len
            t = torch.arange(seq_len, device=x.device).type_as(self.inv_freq)
            freqs = torch.outer(t, self.inv_freq).to(x.device)
            self.cos_cached = freqs.cos().bfloat16()
            self.sin_cached = freqs.sin().bfloat16()
        return self.cos_cached[None, :, None, :], self.sin_cached[None, :, None, :]

def apply_rotary_emb(x, cos, sin):
    assert x.ndim == 4  # multihead attention
    d = x.shape[3] // 2
    x1 = x[..., :d]
    x2 = x[..., d:]
    y1 = x1 * cos + x2 * sin
    y2 = x1 * (-sin) + x2 * cos
    return torch.cat([y1, y2], 3).type_as(x)

def apply_rotary_emb_5(x, cos, sin):
    assert x.ndim == 5  # multihead attention
    d = x.shape[4] // 2
    x1 = x[..., :d]
    x2 = x[..., d:]
    y1 = x1 * cos + x2 * sin
    y2 = x1 * (-sin) + x2 * cos
    return torch.cat([y1, y2], 4).type_as(x)

class CPLinear(nn.Module):
    # Bilinear form of x using CP decomposition
    def __init__(self, in_features, n_head, head_dim, rank: int = 1, q_rank: int = 12):
        super(CPLinear, self).__init__()
        self.in_features = in_features
        self.n_head = n_head
        self.head_dim = head_dim
        self.rank = rank
        self.q_rank = q_rank

        self.c_q = nn.Linear(in_features, n_head * head_dim, bias=False)
        self.rotary = Rotary(self.head_dim)

        # Define linear transformations for A projections
        self.W_A_k = nn.Linear(in_features, n_head * rank, bias=False)
        self.W_A_v = nn.Linear(in_features, n_head * rank, bias=False)

        # Define B projection parameters for K, V
        self.W_B_k = nn.Linear(in_features, rank * head_dim, bias=False)
        self.W_B_v = nn.Linear(in_features, rank * head_dim, bias=False)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.W_A_k.weight)
        nn.init.xavier_uniform_(self.W_A_v.weight)
        nn.init.xavier_uniform_(self.W_B_k.weight)
        nn.init.xavier_uniform_(self.W_B_v.weight)
        nn.init.xavier_uniform_(self.c_q.weight)

    def forward(self, x):
        batch_size, seq_len, _ = x.size()

        # Compute Q
        q = self.c_q(x).view(batch_size, seq_len, self.n_head, self.head_dim)
        # Apply rotary embeddings
        cos, sin = self.rotary(q)
        q = apply_rotary_emb(q, cos, sin)

        # Compute intermediate variables A for K and V
        A_k = self.W_A_k(x).view(batch_size, seq_len, self.n_head, self.rank)
        A_v = self.W_A_v(x).view(batch_size, seq_len, self.n_head, self.rank)

        # Compute intermediate variables B for K and V
        B_k = self.W_B_k(x).view(batch_size, seq_len, self.rank, self.head_dim)
        B_v = self.W_B_v(x).view(batch_size, seq_len, self.rank, self.head_dim)
        B_k = apply_rotary_emb(B_k, cos, sin)

        # Use torch.matmul to directly compute K and V
        k = torch.matmul(A_k, B_k).div_(self.rank)  # shape: (batch_size, seq_len, n_head, head_dim)
        v = torch.matmul(A_v, B_v).div_(self.rank)  # shape: (batch_size, seq_len, n_head, head_dim)

        return q, k, v

class CausalSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.n_head = config.n_head
        self.head_dim = config.head_dim
        self.n_embd = config.n_embd  # Fixed embedding dimension
        self.rank = config.rank
        self.q_rank = config.q_rank

        # CPLinear projections directly output multi-head dimensions
        self.c_qkv = CPLinear(self.n_embd, self.n_head, self.head_dim, self.rank, self.q_rank)

        # Output projection from (n_head * head_dim) back to n_embd
        self.c_proj = nn.Linear(self.n_head * self.head_dim, self.n_embd, bias=False)
        self.c_proj.weight.data.zero_()
        
        # Add group norm
        self.using_groupnorm = getattr(config, 'using_groupnorm', False)
        if self.using_groupnorm:
            # Apply RMSNorm to each head's output dimension
            self.subln = RMSNorm(self.head_dim, eps=1e-5, elementwise_affine=True)

    def forward(self, x):
        B, T, C = x.size()  # (batch_size, seq_length, n_embd)

        # Project inputs to queries, keys, and values directly with multi-head shape
        q, k, v = self.c_qkv(x)  # Each has shape (B, T, n_head, head_dim)

        # Scaled dot-product attention
        y = F.scaled_dot_product_attention(
            q.transpose(1, 2),  # (B, n_head, T, head_dim)
            k.transpose(1, 2),
            v.transpose(1, 2),
            is_causal=True
        )
        
        if self.using_groupnorm:
            # Apply RMSNorm directly to each head's output
            y = self.subln(y)
        
        y = y.transpose(1, 2).contiguous().view(B, T, self.n_head * self.head_dim)
        y = self.c_proj(y)
        return y

class MLP(nn.Module):

    def __init__(self, config):
        super().__init__()
        # Calculate the floored hidden dimension size
        hidden_dim = math.floor(8 / 3 * config.n_embd)

        # Split the linear projection into two parts for SwiGLU
        self.c_fc1 = nn.Linear(config.n_embd, hidden_dim, bias=False)
        self.c_fc2 = nn.Linear(config.n_embd, hidden_dim, bias=False)

        # Output projection
        self.c_proj = nn.Linear(hidden_dim, config.n_embd, bias=False)
        self.c_proj.weight.data.zero_()  # zero init suggested by @Grad62304977

    def forward(self, x):
        # Apply the first linear layer to produce two projections
        x1 = self.c_fc1(x)
        x2 = self.c_fc2(x)

        # Apply the SwiGLU gating: SILU on one projection, and gate with the other
        x = F.silu(x1) * x2

        # Apply the final output projection
        x = self.c_proj(x)
        return x

class Block(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.attn = CausalSelfAttention(config)
        self.mlp = MLP(config)

    def forward(self, x):
        x = x + self.attn(F.rms_norm(x, (x.size(-1),)))
        x = x + self.mlp(F.rms_norm(x, (x.size(-1),)))
        return x

# -----------------------------------------------------------------------------
# The main GPT-2 model

@dataclass
class GPTConfig(PretrainedConfig):
    model_type = "gpt2"  # 添加模型类型标识
    vocab_size: int = 50304
    n_layer: int = 12
    n_head: int = 22  # Number of attention heads
    head_dim: int = 64  # Dimension per head
    n_embd: int = 768  # Fixed embedding dimension
    rank: int = 2  # CP rank for key and value
    q_rank: int = 12  # CP rank for query
    block_size: int = 1024  # Maximum sequence length
    bias: bool = False  # Use bias in all linear layers
    dropout: float = 0.0  # Dropout rate
    scale_attn_by_inverse_layer_idx: bool = False  # Scale attention by 1/sqrt(layer_idx)
    using_groupnorm: bool = False  # Whether to use Group Layernorm

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
class GPT(PreTrainedModel):
    config_class = GPTConfig
    base_model_prefix = "gpt2"
    supports_gradient_checkpointing = True

    def __init__(self, config):
        # if self is not a subclass of PreTrainedModel, then we need to call super().__init__()
        # else we can just call super().__init__(config) to handle the config argument
        if not isinstance(self, PreTrainedModel):
            super().__init__()
        else:
            super().__init__(config)
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            wte=nn.Embedding(config.vocab_size, config.n_embd),
            h=nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
        ))
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        self.transformer.wte.weight = self.lm_head.weight  # Weight tying

    def forward(self, idx, targets=None, return_logits=True, output_all_seq=False):

        # forward the GPT model itself
        x = self.transformer.wte(idx)  # token embeddings of shape (b, t, n_embd)
        for block in self.transformer.h:
            x = block(x)
        x = F.rms_norm(x, (x.size(-1),))

        if targets is not None:
            # if we are given some desired targets also calculate the loss
            logits = self.lm_head(x)
            logits = logits.float()  # use tf32/fp32 for logits
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
        elif output_all_seq:
            logits = self.lm_head(x[:, :, :]) # note: using list [-1] to preserve the time dim
            loss = None
        else:
            # inference-time mini-optimization: only forward the lm_head on the very last position
            logits = self.lm_head(x[:, [-1], :])  # note: using list [-1] to preserve the time dim
            logits = logits.float()  # use tf32/fp32 for logits
            loss = None

        # there are performance reasons why not returning logits is prudent, if not needed
        if not return_logits:
            logits = None

        return logits, loss
    
    
    def crop_block_size(self, block_size):
        # model surgery to decrease the block size if necessary
        # e.g. we may load the GPT2 pretrained model checkpoint (block size 1024)
        # but want to use a smaller block size for some smaller, simpler model
        # assert block_size <= self.config.block_size
        # self.config.block_size = block_size
        # self.transformer.wpe.weight = nn.Parameter(self.transformer.wpe.weight[:block_size])
        # for block in self.transformer.h:
        #     block.attn.bias = block.attn.bias[:,:,:block_size,:block_size]
        pass
                
    def estimate_mfu(self, fwdbwd_per_iter, dt):
        """ estimate model flops utilization (MFU) in units of A100 bfloat16 peak FLOPS """
        # first estimate the number of flops we do per iteration.
        # see PaLM paper Appendix B as ref: https://arxiv.org/abs/2204.02311
        N = self.get_num_params()
        cfg = self.config
        L, H, Q, T = cfg.n_layer, cfg.n_head, cfg.n_embd//cfg.n_head, cfg.block_size
        flops_per_token = 6*N + 12*L*H*Q*T
        flops_per_fwdbwd = flops_per_token * T
        flops_per_iter = flops_per_fwdbwd * fwdbwd_per_iter
        # express our flops throughput as ratio of A100 bfloat16 peak flops
        flops_achieved = flops_per_iter * (1.0/dt) # per second
        flops_promised = 312e12 # A100 GPU bfloat16 peak flops is 312 TFLOPS
        mfu = flops_achieved / flops_promised
        return mfu
    def get_num_params(self, non_embedding=True):
        """
        Return the number of parameters in the model.
        For non-embedding count (default), the position embeddings get subtracted.
        The token embeddings would too, except due to the parameter sharing these
        params are actually used as weights in the final layer, so we include them.
        """
        n_params = sum(p.numel() for p in self.parameters())
        # if non_embedding:
        #     n_params -= self.transformer.wpe.weight.numel()
        # return n_params
        return n_params
    

    # 添加保存和加载配置的方法
    def save_pretrained(self, save_directory):
        self.config.save_pretrained(save_directory)
        super().save_pretrained(save_directory, safe_serialization=False)

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path, *model_args, **kwargs):
        config = kwargs.pop("config", None)
        if config is None:
            config = cls.config_class.from_pretrained(pretrained_model_name_or_path, **kwargs)
        model = super().from_pretrained(pretrained_model_name_or_path, config=config, *model_args, **kwargs)
        return model