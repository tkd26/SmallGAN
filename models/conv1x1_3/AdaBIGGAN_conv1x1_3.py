#this class is trying to do the same thig as the author's implementation
# https://github.com/nogu-atsu/SmallGAN/blob/2293700dce1e2cd97e25148543532814659516bd/gen_models/ada_generator.py#L242-L294 
import torch
import torchvision
import torch.nn as nn
from torch.nn.utils.weight_norm import weight_norm

class AdaBIGGAN(nn.Module):
    def __init__(self, config, generator, dataset_size, embed_dim=120, shared_embed_dim = 128,cond_embed_dim = 20,embedding_init="zero", per_groups=0):
        super(AdaBIGGAN,self).__init__()

        self.config = config
        self.per_groups = per_groups
        print('per_groups',per_groups)
        self.generator = generator
        #same as z in the chainer implementation
        self.embeddings = nn.Embedding(dataset_size, embed_dim)
        if embedding_init == "zero":
            self.embeddings.from_pretrained(torch.zeros(dataset_size,embed_dim),freeze=False)

        '''
        -- 初期レイヤのスケールとバイアス --
        '''
        in_channels = self.generator.blocks[0][0].conv1.in_channels
        self.bsa_linear_scale = torch.nn.Parameter(torch.ones(in_channels,))
        self.bsa_linear_bias = torch.nn.Parameter(torch.zeros(in_channels,))
        
        '''
        -- embedding用のzの定義 --
            in_channels：zを入力する中間レイヤのチャネルのリスト
            num_slots：zを分割する数（入力＋挿入する中間レイヤ数）
            z_chunk_size：zベクトルを分割した時の，1つのベクトルのサイズ（割り切れるように）
            dim_z：num_slots数で割り切れるzのサイズ
        '''
        in_channels = [64 * item for item in [16, 16, 8, 4]]
        self.num_slots = len(in_channels) + 1 # 5
        self.z_chunk_size = (self.config['dim_z'] // self.num_slots) # 120 // 5 = 24
        self.dim_z = self.z_chunk_size *  self.num_slots

        '''
        -- conv1x1のパラメータを生成するFC層の定義 --
            in_size：FC層へ与えられるzベクトルのサイズ
            conv1x1_paramG_weights：conv1x1の重みを生成するFC層のリスト
            conv1x1_paramG_biases：conv1x1のバイアスを生成するFC層のリスト
            conv1x1_first_paramG_weight：最初のconv1x1の重みを生成するFC層のリスト
            conv1x1_first_paramG_bias：最初のconv1x1のバイアスを生成するFC層のリスト
        '''
        in_size = 148
        self.conv1x1_paramG_weights = []
        self.conv1x1_paramG_biases = []
        for ch in [1536, 1536, 1536, 768, 768, 384, 384, 192, 192, 96]:
            if self.per_groups==0: groups = 1
            else: groups = ch //self.per_groups
            self.conv1x1_paramG_weights += [nn.Linear(int(in_size), int(ch*ch/groups))]
            self.conv1x1_paramG_biases += [nn.Linear(int(in_size), int(ch))]
        self.conv1x1_paramG_weights = nn.ModuleList(self.conv1x1_paramG_weights)
        self.conv1x1_paramG_biases = nn.ModuleList(self.conv1x1_paramG_biases)

        ch_first = self.generator.blocks[0][0].conv1.in_channels
        if self.per_groups==0: groups = 1
        else: groups = ch_first //self.per_groups
        self.conv1x1_first_paramG_weight = nn.Linear(int(in_size), int(ch_first*ch_first/groups))
        self.conv1x1_first_paramG_bias = nn.Linear(int(in_size), int(ch_first))

        '''
        -- zベクトルに付与するconditionalベクトルが通過するlinear --
        * 今回の実験ではconditionを指定しないので使用しない
        '''
        self.linear = nn.Linear(1, shared_embed_dim, bias=False)
        #torch.nn.init.kaiming_normal_(self.linear.weight)
        init_weight = generator.shared.weight.mean(dim=0,keepdim=True).transpose(1,0)
        assert self.linear.weight.data.shape == init_weight.shape
        self.linear.weight.data  = init_weight
        del generator.shared

        '''
        -- 1x1convの定義 --
            conv1x1：1x1convのリスト
            conv1x1_first：最初の1x1conv
        '''
        # blockのconv1x1
        self.conv1x1 = []
        for ch in [1536, 1536, 1536, 768, 768, 384, 384, 192, 192, 96]:
            if self.per_groups==0: groups = 1
            else: groups = ch //self.per_groups
            conv = nn.Conv2d(ch, ch, kernel_size=1, stride=1, padding=0, groups=groups).cuda()
            weight_init = torch.eye(conv.weight.data.shape[0]).unsqueeze(-1).unsqueeze(-1)
            # print(conv.weight.data.shape, conv.bias.data.shape)
            self.conv1x1 += [conv]
        self.conv1x1 = nn.ModuleList(self.conv1x1)

        # 最初のレイヤのconv1x1
        in_ch = self.generator.blocks[0][0].conv1.in_channels
        if self.per_groups==0: groups = 1
        else: groups = in_ch //self.per_groups
        self.conv1x1_first = nn.Conv2d(in_ch, in_ch, kernel_size=1, stride=1, padding=0, groups=groups).cuda()
        
        self.set_training_parameters()
                
    def forward(self, z):
        '''
        z: tensor whose shape is (batch_size, shared_embed_dim) . in the training time noise (`epsilon` in the original paper) should be added. 
        '''
        #my note
        #here, we *do* make `y` inside forwad function
        #`y` is equivalent to `c` in chainer smallgan repo

        '''
        -- zベクトルと各レイヤへ与えるベクトルの生成 --
            y：conditionベクトル（バッチサイズ*128）
            z：zベクトル
            ys：各レイヤへ与えるベクトル
        '''
        # conditionベクトルの生成
        y = torch.ones((z.shape[0], 1),dtype=torch.float32,device=z.device)#z.shape[0] is batch size
        y = self.linear(y) # batch * 128

        # If hierarchical (i.e. use different z per layer), concatenate zs and ys
        if self.generator.hier:
            zs = torch.split(z, self.generator.z_chunk_size, 1) # 6つに分割(1つあたり20)
            z_origin = z
            z = zs[0] # batch * 20
            ys = [torch.cat([y, item], 1) for item in zs[1:]] # リスト一つの要素サイズはbatch * (128+20)

            # ys = zs[1:]
            # ys = [z_origin]*5

            # ys = zs[1]
            # for i in zs[2:]:
            #     ys = torch.cat((ys,i),1)
            # ys = [ys]*5

            # ys = [z]*5
            
            # ys = [y] * 5

        else:
            raise NotImplementedError("I don't implement this case")
            ys = [y] * len(self.generator.blocks)

        '''
        -- 最初の1x1conv --
        '''
        # First linear layer
        h = self.generator.linear(z)
        # Reshape
        h = h.view(h.size(0), -1, self.generator.bottom_width, self.generator.bottom_width)
        h = h*self.bsa_linear_scale.view(1,-1,1,1) + self.bsa_linear_bias.view(1,-1,1,1) 
        # h = self.conv1x1_first(h)

        # conv1x1_first_weight = (self.conv1x1_first_paramG_weight(ys[1]) + 1).view(h.shape[0],h.shape[1],-1).unsqueeze(-1).unsqueeze(-1)
        # conv1x1_first_bias = self.conv1x1_first_paramG_bias(ys[1]).squeeze(0)
        # conv1x1_first_weight = conv1x1_first_weight.repeat(1,1,1,h.shape[2],h.shape[3])
        # conv1x1_first_bias = conv1x1_first_bias.unsqueeze(-1).unsqueeze(-1).repeat(1,1,h.shape[2],h.shape[3])
        # h = h.unsqueeze(2) * conv1x1_first_weight
        # h = torch.sum(h, dim=2).squeeze(2)
        # h += conv1x1_first_bias
        
        '''
        -- 2層目以降の処理 --
        '''
        i = 0
        for index, blocklist in enumerate(self.generator.blocks):
            # Second inner loop in case block has multiple layers
            for block_idx, block in enumerate(blocklist):
                if block_idx==0:
                    # print(i)
                    # groups=chの時はweightに+1する
                    # conv1x1_1_weight = (self.conv1x1_paramG_weights[i](ys[index]) + 1).view(h.shape[0],h.shape[1],-1).unsqueeze(-1).unsqueeze(-1)
                    conv1x1_1_weight = (self.conv1x1_paramG_weights[i](ys[index])).view(h.shape[0],h.shape[1],-1).unsqueeze(-1).unsqueeze(-1)
                    if self.per_groups != 0:
                        weight_init = torch.eye(self.per_groups).unsqueeze(-1).unsqueeze(-1).unsqueeze(0)
                        weight_init = weight_init.repeat(conv1x1_1_weight.shape[0], conv1x1_1_weight.shape[1]//self.per_groups, 1, 1, 1)
                    else:
                        weight_init = torch.eye(conv1x1_1_weight.shape[1]).unsqueeze(-1).unsqueeze(-1).unsqueeze(0)
                        # weight_init = weight_init.repeat(conv1x1_1_weight.shape[0], conv1x1_1_weight.shape[1], 1, 1, 1)
                    conv1x1_1_weight += weight_init.cuda()
                    conv1x1_1_bias = self.conv1x1_paramG_biases[i](ys[index]).squeeze(0)

                    x = h
                    h = block.bn1(x, ys[index])

                    # h = self.conv1x1[i](h)の代わりの計算
                    conv1x1_1_weight = conv1x1_1_weight.repeat(1,1,1,h.shape[2],h.shape[3])
                    conv1x1_1_bias = conv1x1_1_bias.unsqueeze(-1).unsqueeze(-1).repeat(1,1,h.shape[2],h.shape[3])
                    h = h.unsqueeze(2) * conv1x1_1_weight
                    h = torch.sum(h, dim=2).squeeze(2)
                    h += conv1x1_1_bias

                    h = block.activation1(h)
                    h = block.upsample(h)
                    x = block.upsample(x)
                    h = block.conv1(h)

                    # print(i+1)
                    # conv1x1_2_weight = (self.conv1x1_paramG_weights[i+1](ys[index]) + 1).view(h.shape[0],h.shape[1],-1).unsqueeze(-1).unsqueeze(-1)
                    conv1x1_2_weight = (self.conv1x1_paramG_weights[i+1](ys[index])).view(h.shape[0],h.shape[1],-1).unsqueeze(-1).unsqueeze(-1)
                    if self.per_groups != 0:
                        weight_init = torch.eye(self.per_groups).unsqueeze(-1).unsqueeze(-1).unsqueeze(0)
                        weight_init = weight_init.repeat(conv1x1_2_weight.shape[0], conv1x1_2_weight.shape[1]//self.per_groups, 1, 1, 1)
                    else:
                        weight_init = torch.eye(conv1x1_2_weight.shape[1]).unsqueeze(-1).unsqueeze(-1).unsqueeze(0)
                        # weight_init = weight_init.repeat(conv1x1_2_weight.shape[0], conv1x1_2_weight.shape[1], 1, 1, 1)
                    conv1x1_2_weight += weight_init.cuda()
                    conv1x1_2_bias = self.conv1x1_paramG_biases[i+1](ys[index]).squeeze(0)

                    h = block.bn2(h, ys[index])

                    # h = self.conv1x1[i+1](h)の代わりの計算
                    conv1x1_2_weight = conv1x1_2_weight.repeat(1,1,1,h.shape[2],h.shape[3])
                    conv1x1_2_bias = conv1x1_2_bias.unsqueeze(-1).unsqueeze(-1).repeat(1,1,h.shape[2],h.shape[3])
                    h = h.unsqueeze(2) * conv1x1_2_weight
                    h = torch.sum(h, dim=2).squeeze(2)
                    h += conv1x1_2_bias

                    h = block.activation2(h)
                    h = block.conv2(h) 
                    x = block.conv_sc(x)
                    h = h + x

                    i += 2
                elif block_idx==1: # Attentionの場合
                    h = block(h)

        # Apply batchnorm-relu-conv-tanh at output
        return torch.tanh(self.generator.output_layer(h))
    

    
    def set_training_parameters(self):
        '''
        set requires_grad=True only for parameters to be updated, requires_grad=False for others.
        '''
        #set all parameters requires_grad=False first
        for param in self.parameters():
            param.requires_grad = False
            
        named_params_requires_grad = {}
        # --最初以降のbnのパラメータ生成（1x1convでは使用しない）
        # named_params_requires_grad.update(self.batch_stat_gen_params()) 
        # --最初のlinear層
        named_params_requires_grad.update(self.linear_gen_params()) 
        # --最初のlinear層のパラメータ
        named_params_requires_grad.update(self.bsa_linear_params())
        # --bnのパラメータ生成に使うベクトルを入れるlinear
        named_params_requires_grad.update(self.calss_conditional_embeddings_params())
        # --ベクトルをembeddingする
        named_params_requires_grad.update(self.embeddings_params())

        # --1x1convのパラメータを生成するFC層
        named_params_requires_grad.update(self.conv1x1_paramG_weights_params())
        named_params_requires_grad.update(self.conv1x1_paramG_biases_params())
        # --1x1conv_firstのパラメータを生成するFC層
        named_params_requires_grad.update(self.conv1x1_first_paramG_weight_params())
        named_params_requires_grad.update(self.conv1x1_first_paramG_bias_params())
        
        for name,param in named_params_requires_grad.items():
            param.requires_grad = True

    def conv1x1_paramG_weights_params(self):
        '''
        '''
        named_params = {}
        for i,j in enumerate(self.conv1x1_paramG_weights):
            for name, value in j.named_parameters():
                name = 'conv1x1_paramG_weights.' + str(i) + '.' + name
                named_params[name] = value
        return named_params

    def conv1x1_paramG_biases_params(self):
        '''
        '''
        named_params = {}
        for i,j in enumerate(self.conv1x1_paramG_biases):
            for name, value in j.named_parameters():
                name = 'conv1x1_paramG_biases.' + str(i) + '.' + name
                # print(name)
                named_params[name] = value
        return named_params

    def conv1x1_first_paramG_weight_params(self):
        return {
            "conv1x1_first_paramG_weight.weight":self.conv1x1_first_paramG_weight.weight,
            "conv1x1_first_paramG_weight.bias":self.conv1x1_first_paramG_weight.bias}

    def conv1x1_first_paramG_bias_params(self):
        return {
            "conv1x1_first_paramG_bias.weight":self.conv1x1_first_paramG_bias.weight,
            "conv1x1_first_paramG_bias.bias":self.conv1x1_first_paramG_bias.bias}

    def conv1x1_first_params(self):
        return {"conv1x1_first.weight":self.conv1x1_first.weight,"conv1x1_first.bias":self.conv1x1_first.bias}
        # return {
        #     "conv1x1_first.weight_g":self.conv1x1_first.weight_g,
        #     "conv1x1_first.weight_v":self.conv1x1_first.weight_v,
        #     "conv1x1_first.bias":self.conv1x1_first.bias}


    def conv1x1_params(self):
        '''
        conv1x1.0.weight
        conv1x1.0.bias
        conv1x1.1.weight
        conv1x1.1.bias
        conv1x1.2.weight
        conv1x1.2.bias
        conv1x1.3.weight
        conv1x1.3.bias
        conv1x1.4.weight
        conv1x1.4.bias
        conv1x1.5.weight
        conv1x1.5.bias
        conv1x1.6.weight
        conv1x1.6.bias
        conv1x1.7.weight
        conv1x1.7.bias
        conv1x1.8.weight
        conv1x1.8.bias
        conv1x1.9.weight
        conv1x1.9.bias
        '''
        named_params = {}
        for name, value in self.conv1x1.named_parameters():
            name = 'conv1x1.' + name
            # print(name)
            named_params[name] = value
        return named_params

            
    # def batch_stat_gen_params(self):
    #     '''
    #     get named parameters to generate batch statistics
    #     Weight corresponding to "Hyper" in Chainer implementation 
    #     ```
    #         blocks.0.0.bn1.gain.weight torch.Size([1536, 148])
    #         blocks.0.0.bn1.bias.weight torch.Size([1536, 148])
    #         blocks.0.0.bn2.gain.weight torch.Size([1536, 148])
    #         blocks.0.0.bn2.bias.weight torch.Size([1536, 148])
    #         blocks.1.0.bn1.gain.weight torch.Size([1536, 148])
    #         blocks.1.0.bn1.bias.weight torch.Size([1536, 148])
    #         blocks.1.0.bn2.gain.weight torch.Size([768, 148])
    #         blocks.1.0.bn2.bias.weight torch.Size([768, 148])
    #         blocks.2.0.bn1.gain.weight torch.Size([768, 148])
    #         blocks.2.0.bn1.bias.weight torch.Size([768, 148])
    #         blocks.2.0.bn2.gain.weight torch.Size([384, 148])
    #         blocks.2.0.bn2.bias.weight torch.Size([384, 148])
    #         blocks.3.0.bn1.gain.weight torch.Size([384, 148])
    #         blocks.3.0.bn1.bias.weight torch.Size([384, 148])
    #         blocks.3.0.bn2.gain.weight torch.Size([192, 148])
    #         blocks.3.0.bn2.bias.weight torch.Size([192, 148])
    #         blocks.4.0.bn1.gain.weight torch.Size([192, 148])
    #         blocks.4.0.bn1.bias.weight torch.Size([192, 148])
    #         blocks.4.0.bn2.gain.weight torch.Size([96, 148])
    #         blocks.4.0.bn2.bias.weight torch.Size([96, 148])
    #     ```
    #     '''
    #     named_params = {}
    #     for name,value in self.named_modules():
    #         if name.split(".")[-1] in ["gain","bias"]:
    #             for name2,value2 in  value.named_parameters():
    #                 name = name+"."+name2
    #                 params = value2
    #                 named_params[name] = params
                    
    #     return named_params
       
    def linear_gen_params(self):
        '''
        Fully connected weights in generator
        finetune with very small learning rate
        ```
            linear.weight torch.Size([24576, 20])
            linear.bias torch.Size([24576])
        ```
        '''
        return {"generator.linear.weight":self.generator.linear.weight,
                       "generator.linear.bias":self.generator.linear.bias}

    def bsa_linear_params(self):
        '''
        Statistics parameter (scale and bias) after lienar layer
        This is a newly intoroduced training parameters that did not exist in the original generator
        '''
        return {"bsa_linear_scale":self.bsa_linear_scale,"bsa_linear_bias":self.bsa_linear_bias}

    def calss_conditional_embeddings_params(self):
        '''
        128 dim input as the conditional noise (?)
        '''
        return {"linear.weight":self.linear.weight}


    def embeddings_params(self):
        '''
        initialized with zero but added with random epsilon for training time
        this is 120 in the BigGAN 128 x 128 while 140 in 256 x 256
        '''
        return  {"embeddings.weight":self.embeddings.weight}

    
if __name__ == "__main__":
    import sys
    sys.path.append("../official_biggan_pytorch/")
    sys.path.append("../")
    from official_biggan_pytorch import utils
    
    import torch
    import torchvision

    parser = utils.prepare_parser()
    parser = utils.add_sample_parser(parser)
    config = vars(parser.parse_args(args=[]))
    
    # taken from https://github.com/ajbrock/BigGAN-PyTorch/issues/8
    config["resolution"] = utils.imsize_dict["I128_hdf5"]
    config["n_classes"] = utils.nclass_dict["I128_hdf5"]
    config["G_activation"] = utils.activation_dict["inplace_relu"]
    config["D_activation"] = utils.activation_dict["inplace_relu"]
    config["G_attn"] = "64"
    config["D_attn"] = "64"
    config["G_ch"] = 96
    config["D_ch"] = 96
    config["hier"] = True
    config["dim_z"] = 120
    config["shared_dim"] = 128
    config["G_shared"] = True
    config = utils.update_config_roots(config)
    config["skip_init"] = True
    config["no_optim"] = True
    config["device"] = "cuda"

    # Seed RNG.
    utils.seed_rng(config["seed"])

    # Set up cudnn.benchmark for free speed.
    torch.backends.cudnn.benchmark = True

    # Import the model.
    model = __import__(config["model"])
    experiment_name = utils.name_from_config(config)
    G = model.Generator(**config).to(config["device"])
    utils.count_parameters(G)

    # Load weights.
    weights_path = "../data/G_ema.pth"  # Change this.
    # weights_path = "./data/G.pth"  # Change this.
    G.load_state_dict(torch.load(weights_path))

    model = AdaBIGGAN(config,G,dataset_size=42)
    model = model.cuda()
    # print(model)
    
    batch_size = 4
    
    z = torch.ones((batch_size,140)).cuda()

    output = model(z)
    
    assert output.shape == (batch_size,3,128,128)
    
    print("simple test pased!")