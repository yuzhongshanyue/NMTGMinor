import numpy as np
import torch, math
import torch.nn as nn
import torch.nn.functional as F
from onmt.modules.Transformer.Layers import PositionalEncoding
from onmt.modules.Transformer.Layers import EncoderLayer, DecoderLayer
from onmt.modules.StochasticTransformer.Layers import StochasticEncoderLayer, StochasticDecoderLayer
from onmt.modules.Transformer.Models import TransformerEncoder, TransformerDecoder
import onmt
from onmt.modules.WordDrop import embedded_dropout
from onmt.modules.Transformer.Layers import XavierLinear, MultiHeadAttention, FeedForward, PrePostProcessing
Linear = XavierLinear
from onmt.modules.Utilities import mean_with_mask_backpropable as mean_with_mask
from onmt.modules.Utilities import max_with_mask

import copy
"""
    Variational Inference for model depth generation

    Our model structure is generated by a latent variable z = {z_1, z_2 .... z_n} corresponding to n layers
    Assumption is each layer is generated randomly (motivated by the Stochastic Network)
    Mean Field assumption is used (one set of parameters for each z)

    Our loss function is:

    L = E_q_z ( log (p (Y|X, z)) - KL( q(z|X, y) || p(z|X))
    (data likelihood given the latent variable)
    
    The Prior model estimates p(z | x)

    The Posterior model estimates q(z | x, y)

    During training we take the sample from posterior (variational inference)
    During testing  y is not available, so we use the prior (conditional prior)

"""



"""
    The Prior model estimates p(z | x)
"""
class NeuralPrior(nn.Module):

    """Encoder in 'Attention is all you need'
    
    Args:
        opt: list of options ( see train.py )
        dicts : dictionary (for source language)
        
    """
    def __init__(self, opt, embedding, positional_encoder):
    
        super(NeuralPrior, self).__init__()

        encoder_opt = copy.deepcopy(opt)
        # quick_hack to override some hyper parameters of the prior encoder
        encoder_opt.layers = opt.layers
        self.dropout = opt.dropout
        self.opt = opt

        self.var_ignore_first_source_token = opt.var_ignore_first_source_token

        self.encoder = TransformerEncoder(encoder_opt, embedding, positional_encoder)

        # self.projector = Linear(opt.model_size, opt.model_size)
        # self.mean_predictor = Linear(opt.model_size, opt.model_size)
        # self.var_predictor = Linear(opt.model_size, opt.model_size)

        # for each layer, we define
        # a set of transformation linear
        self.projector = nn.ModuleList()
        self.mean_predictor = nn.ModuleList()
        self.var_predictor = nn.ModuleList()

        self.context_projector = nn.ModuleList()

        for i in range(self.opt.layers):
            self.mean_predictor.append(Linear(opt.model_size, opt.model_size, weight_norm=False))
            self.var_predictor.append(Linear(opt.model_size, opt.model_size, weight_norm=False))
            self.context_projector.append(Linear(opt.model_size, opt.model_size, weight_norm=False))




    def forward(self, input, context, **kwargs):
        """
        Inputs Shapes: 
            input: batch_size x len_src (wanna tranpose)
        
        Outputs Shapes:
            out: batch_size x len_src x d_model
            mask_src 
            
        """
        if self.var_ignore_first_source_token:
            input = input[:,1:]
        # pass the input to the transformer encoder (we also return the mask)
        context_stack, _ = self.encoder(input, freeze_embedding=False, return_stack=True)
        
        # print(len(context_stack))   
        # Now we have to mask the context with zeros
        # context size: T x B x H
        # mask size: T x B x 1 for broadcasting

        mask = input.eq(onmt.Constants.PAD).transpose(0, 1).unsqueeze(2)

        
        contexts = list()

        # output list:
        # contain a list of vector 
        # and list of distribution
        encoder_meaning = list()
        p_z = list()
        for i, context_ in enumerate(context_stack):

            # relu and 
            context_ = torch.relu(self.context_projector[i](context_)) + context_

            context = mean_with_mask(context_, mask)
            # context = torch.tanh(self.context_projector[i](mean_context))
            encoder_meaning.append(context)
            # context = torch.tanh(self.projector(context))
            # contexts.append(mean_context)
            mean = self.mean_predictor[i](context)
            var = self.var_predictor[i] (context)
            # mean = mean_context
            # var = mean_context
            var = torch.exp(0.5*var)
            p_z_ = torch.distributions.normal.Normal(mean.float(), var.float())
            p_z.append(p_z_)


        # return a list of prior distribution P(z | X) for each layer
        return encoder_meaning, p_z


class NeuralPosterior(nn.Module):

    """Neural Posterior using Transformer
    
    Args:
        opt: list of options ( see train.py )
        embedding : dictionary (for target language)
        
    """
    
    def __init__(self, opt, embedding, positional_encoder, prior=None):
    
        super(NeuralPosterior, self).__init__()
        
        encoder_opt = copy.deepcopy(opt)
        self.opt = opt

        # quick_hack to override some hyper parameters of the prior encoder
        encoder_opt.layers = opt.layers
        self.dropout = opt.dropout

        self.var_ignore_first_target_token = opt.var_ignore_first_target_token
        self.var_ignore_first_source_token = opt.var_ignore_first_source_token

        self.posterior_combine = opt.var_posterior_combine
        
        # if opt.var_posterior_share_weight == True:
        #     assert prior is not None
        #     self.encoder = prior.encoder
        # else:
        self.encoder = TransformerEncoder(encoder_opt, embedding, positional_encoder)

        self.projector = nn.ModuleList()
        self.mean_predictor = nn.ModuleList()
        self.var_predictor = nn.ModuleList()

        for i in range(self.opt.layers):

            if opt.var_posterior_combine == 'concat':
                self.projector.append(Linear(opt.model_size * 1, opt.model_size))
            elif opt.var_posterior_combine == 'sum':
                self.projector.append(Linear(opt.model_size * 1, opt.model_size))
            else:
                raise NotImplementedError

            self.mean_predictor.append(Linear(opt.model_size, opt.model_size))
            self.var_predictor.append(Linear(opt.model_size, opt.model_size))
    
    def forward(self, encoder_meaning, input_src, input_tgt, **kwargs):
        """
        Inputs Shapes: 
            input: batch_size x len_src (wanna tranpose)
        
        Outputs Shapes:
            out: batch_size x len_src x d_model
            mask_src 
            
        """

        """ Embedding: batch_size x len_src x d_model """

        if self.var_ignore_first_target_token:
            input_tgt = input_tgt[:,1:]
        
        if self.var_ignore_first_source_token:
            input_src = input_src[:,1:]
        # encoder_context = encoder_context.detach()
        decoder_context, _ = self.encoder(input_tgt, freeze_embedding=True, return_stack=True)
        # encoder_context, _ = self.encoder(input_src, freeze_embedding=True, return_stack=True)

        # src_mask = input_src.eq(onmt.Constants.PAD).transpose(0, 1).unsqueeze(2)
        tgt_mask = input_tgt.eq(onmt.Constants.PAD).transpose(0, 1).unsqueeze(2) 


        # take the mean of each context
        # encoder_context = encoder_meaning
        q_z = list()

        for i, decoder_context_ in enumerate(decoder_context):

            decoder_context_ = mean_with_mask(decoder_context_, tgt_mask)
            # encoder_context_ = mean_with_mask(encoder_context[i], src_mask)

            # if self.posterior_combine == 'concat':
            #     context = torch.cat([encoder_context_, decoder_context_], dim=-1)
            # elif self.posterior_combine == 'sum':
            #     context = encoder_context_ + decoder_context_
            context = decoder_context_

            context = F.elu(self.projector[i](context))

            mean = self.mean_predictor[i](context)
            log_var = self.var_predictor[i](context)
            var = torch.exp(0.5 * log_var)

            q_z_ = torch.distributions.normal.Normal(mean.float(), var.float())

            q_z.append(q_z_)
        # return distribution Q(z | X, Y)
        return q_z
