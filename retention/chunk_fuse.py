# -*- coding: utf-8 -*-

import torch
import triton
import triton.language as tl


@triton.jit
def chunk_retention_fwd_kernel(
    q,
    k,
    v,
    o,
    b,
    s_qh,
    s_qt,
    s_qd,
    s_oh,
    H,
    T,
    scale,
    BT: tl.constexpr,
    BD: tl.constexpr,
    DK: tl.constexpr,
    DV: tl.constexpr
):
    i_v, i_k, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    p_b = b + i_bh % H

    o_i = tl.arange(0, BT)
    b_b = tl.load(p_b)
    d_b, d_o, d_h = tl.math.exp2(BT * b_b), tl.math.exp2(o_i * b_b), tl.math.exp2((BT - o_i) * b_b)
    # [BT, BT]
    m_s = o_i[:, None] >= o_i[None, :]
    d_s = tl.where(m_s, tl.math.exp2((o_i[:, None] - o_i[None, :]) * b_b), 0)
    # [DK, DV]
    b_h = tl.zeros([DK, DV], dtype=tl.float32)
    for i in range(0, tl.cdiv(T, BT)):
        p_q = tl.make_block_ptr(q + i_bh * s_qh, (T, BD), (s_qt, s_qd), (i * BT, i_k * DK), (BT, DK), (1, 0))
        p_k = tl.make_block_ptr(k + i_bh * s_qh, (BD, T), (s_qd, s_qt), (i_k * DK, i * BT), (DK, BT), (0, 1))
        p_v = tl.make_block_ptr(v + i_bh * s_qh, (T, BD), (s_qt, s_qd), (i * BT, i_v * DV), (BT, DV), (1, 0))
        p_o = tl.make_block_ptr(o + i_bh * s_oh + i_k * s_qh, (T, BD), (s_qt, s_qd), (i * BT, i_v * DV), (BT, DV), (1, 0))

        # [BT, DK]
        b_q = tl.load(p_q)
        b_q = (b_q * scale).to(b_q.dtype)
        # [DK, BT]
        b_k = tl.load(p_k)
        # [BT, DV]
        b_v = tl.load(p_v)

        b_s = tl.dot(b_q, b_k, allow_tf32=False) * d_s
        # [BT, DV]
        b_o = tl.dot((b_q * d_o[:, None]).to(b_q.dtype), b_h.to(b_q.dtype), allow_tf32=False)
        b_o += tl.dot(b_s.to(b_q.dtype), b_v, allow_tf32=False)
        # [DK, DV]
        b_h = d_b * b_h + tl.dot(b_k, (b_v * d_h[:, None]).to(b_k.dtype), allow_tf32=False)

        tl.store(p_o, b_o.to(p_o.dtype.element_ty), boundary_check=(0,))


@triton.jit
def chunk_retention_bwd_kernel(
    q,
    k,
    v,
    b,
    do,
    dq,
    dk,
    dv,
    s_qh,
    s_qt,
    s_qd,
    s_dk,
    s_dv,
    H,
    T,
    scale,
    BT: tl.constexpr,
    BD: tl.constexpr,
    DK: tl.constexpr,
    DV: tl.constexpr
):
    i_v, i_k, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    p_b = b + i_bh % H

    o_i = tl.arange(0, BT)
    b_b = tl.load(p_b)
    d_b = tl.math.exp2(BT * b_b)
    d_q, d_k = tl.math.exp2(o_i * b_b) * scale, tl.math.exp2((BT - o_i) * b_b)
    # [BT, BT]
    m_s = o_i[:, None] >= o_i[None, :]
    d_s = tl.where(m_s, tl.math.exp2((o_i[:, None] - o_i[None, :]) * b_b), 0) * scale
    # [DV, DK]
    b_h = tl.zeros([DV, DK], dtype=tl.float32)
    for i in range(0, tl.cdiv(T, BT)):
        p_k = tl.make_block_ptr(k + i_bh * s_qh, (T, BD), (s_qt, s_qd), (i * BT, i_k * DK), (BT, DK), (1, 0))
        p_v = tl.make_block_ptr(v + i_bh * s_qh, (BD, T), (s_qd, s_qt), (i_v * DV, i * BT), (DV, BT), (0, 1))
        p_do = tl.make_block_ptr(do + i_bh * s_qh, (T, BD), (s_qt, s_qd), (i * BT, i_v * DV), (BT, DV), (1, 0))
        p_dq = tl.make_block_ptr(dq + i_bh * s_dk + i_v * s_qh, (T, BD), (s_qt, s_qd), (i * BT, i_k * DK), (BT, DK), (1, 0))

        # [BT, DK]
        b_k = tl.load(p_k)
        # [DV, BT]
        b_v = tl.load(p_v)
        # [BT, DV]
        b_do = tl.load(p_do)
        b_dd = (b_do * d_q[:, None]).to(b_do.dtype)

        # [BT, BT]
        b_ds = tl.dot(b_do, b_v, allow_tf32=False)
        b_ds = (b_ds * d_s).to(b_k.dtype)
        # [BT, DK]
        b_dq = tl.dot(b_dd, b_h.to(b_k.dtype), allow_tf32=False) + tl.dot(b_ds, b_k, allow_tf32=False)
        # [DV, DK]
        b_h = d_b * b_h + tl.dot((b_v * d_k[None, :]).to(b_k.dtype), b_k, allow_tf32=False)

        tl.store(p_dq, b_dq.to(p_dq.dtype.element_ty))

    d_s = tl.trans(d_s)
    # [DK, DV]
    b_dh = tl.zeros([DK, DV], dtype=tl.float32)
    for i in range(1, tl.cdiv(T, BT) + 1):
        p_q = tl.make_block_ptr(q + i_bh * s_qh, (BD, T), (s_qd, s_qt), (i_k * DK, T - i * BT), (DK, BT), (0, 1))
        p_k = tl.make_block_ptr(k + i_bh * s_qh, (T, BD), (s_qt, s_qd), (T - i * BT, i_k * DK), (BT, DK), (1, 0))
        p_v = tl.make_block_ptr(v + i_bh * s_qh, (T, BD), (s_qt, s_qd), (T - i * BT, i_v * DV), (BT, DV), (1, 0))
        p_do = tl.make_block_ptr(do + i_bh * s_qh, (T, BD), (s_qt, s_qd), (T - i * BT, i_v * DV), (BT, DV), (1, 0))
        p_dk = tl.make_block_ptr(dk + i_bh*s_dk + i_v*s_qh, (T, BD), (s_qt, s_qd), (T - i * BT, i_k * DK), (BT, DK), (1, 0))
        p_dv = tl.make_block_ptr(dv + i_bh*s_dv + i_k*s_qh, (T, BD), (s_qt, s_qd), (T - i * BT, i_v * DV), (BT, DV), (1, 0))
        # [DK, BT]
        b_q = tl.load(p_q)
        # [BT, DK]
        b_k = tl.load(p_k)
        # [BT, DV]
        b_v = tl.load(p_v)
        b_do = tl.load(p_do)
        b_dd = (b_do * d_q[:, None]).to(b_do.dtype)

        # [BT, BT]
        b_ds = tl.dot(b_v, tl.trans(b_do), allow_tf32=False)
        b_ds = (b_ds * d_s).to(b_k.dtype)

        # [BT, BT]
        b_s = tl.dot(b_k, b_q, allow_tf32=False) * d_s
        # [BT, DK]
        b_dk = tl.dot(b_v, tl.trans(b_dh).to(b_v.dtype), allow_tf32=False) * d_k[:, None]
        b_dk += tl.dot(b_ds, tl.trans(b_q), allow_tf32=False)
        # [BT, DV]
        b_dv = tl.dot(b_k, b_dh.to(b_k.dtype), allow_tf32=False) * d_k[:, None]
        b_dv += tl.dot(b_s.to(b_q.dtype), b_do, allow_tf32=False)
        # [DK, DV]
        b_dh = d_b * b_dh + tl.dot(b_q, b_dd, allow_tf32=False)

        tl.store(p_dk, b_dk.to(p_dk.dtype.element_ty))
        tl.store(p_dv, b_dv.to(p_dv.dtype.element_ty))


class ChunkRetentionFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, q, k, v):
        batch_size, n_heads, seq_len, d_head = q.shape
        scale = d_head ** -0.5
        BD = triton.next_power_of_2(q.shape[-1])
        BT = 32 if BD > 64 else 64
        DK, DV = min(BD, 128), min(BD, 64)
        NK, NV = triton.cdiv(BD, DK), triton.cdiv(BD, DV)
        num_stages = 3 if d_head <= 64 else 2
        num_warps = 4

        def pad(x, sizes):
            p = x.new_zeros(sizes)
            p[tuple(slice(0, i) for i in x.shape)] = x
            return p
        if BD != d_head:
            q, k, v = (pad(i, (batch_size, n_heads, seq_len, BD)) for i in (q, k, v))

        o = q.new_empty(batch_size, n_heads, NK * seq_len, BD)
        # NOTE: be careful about BF16 precision
        b = (1. - q.new_tensor(2., dtype=torch.float).pow(-5 - q.new_tensor(range(n_heads), dtype=torch.float))).log2()
        grid = (NV, NK, batch_size * n_heads)
        chunk_retention_fwd_kernel[grid](
            q, k, v, o, b,
            q.stride(1), q.stride(2), q.stride(3), o.stride(1),
            n_heads, seq_len, scale,
            BT=BT, BD=BD, DK=DK, DV=DV,
            num_warps=num_warps,
            num_stages=num_stages
        )
        o = o.view(batch_size, n_heads, NK, seq_len, BD).sum(2)
        ctx.save_for_backward(q, k, v, b)
        ctx.batch_size = batch_size
        ctx.n_heads = n_heads
        ctx.seq_len = seq_len
        ctx.d_head = d_head
        ctx.scale = scale
        return o[..., :d_head]

    @staticmethod
    def backward(ctx, do):
        q, k, v, b = ctx.saved_tensors
        scale = ctx.scale
        BD = triton.next_power_of_2(q.shape[-1])
        BT = 64
        DK, DV = min(BD, 64), min(BD, 128)
        NK, NV = triton.cdiv(BD, DK), triton.cdiv(BD, DV)
        batch_size, n_heads, seq_len, d_head = ctx.batch_size, ctx.n_heads, ctx.seq_len, ctx.d_head
        num_stages = 3 if d_head <= 64 else 2
        num_warps = 4
        assert seq_len % BT == 0, f"seq_len {seq_len} must be divisible by block_size {BT}"

        def pad(x, sizes):
            p = x.new_zeros(sizes)
            p[tuple(slice(0, i) for i in x.shape)] = x
            return p
        if BD != d_head:
            do = pad(do, q.shape)

        dq = q.new_empty(batch_size, n_heads, NV * seq_len, BD)
        dk = q.new_empty(batch_size, n_heads, NV * seq_len, BD)
        dv = q.new_empty(batch_size, n_heads, NK * seq_len, BD)
        grid = (NV, NK, batch_size * n_heads)
        chunk_retention_bwd_kernel[grid](
            q, k, v, b, do, dq, dk, dv,
            q.stride(1), q.stride(2), q.stride(3), dk.stride(1), dv.stride(1),
            n_heads, seq_len, scale,
            BT=BT, BD=BD, DK=DK, DV=DV,
            num_warps=num_warps,
            num_stages=num_stages
        )
        dq = dq.view(batch_size, n_heads, NV, seq_len, BD).sum(2)
        dk = dk.view(batch_size, n_heads, NV, seq_len, BD).sum(2)
        dv = dv.view(batch_size, n_heads, NK, seq_len, BD).sum(2)
        return dq[..., :d_head], dk[..., :d_head], dv[..., :d_head]


chunk_retention = ChunkRetentionFunction.apply