
import torch
from torch import nn
from torch.nn import functional as F

class Encoder(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers,cond_dim=256,dropout_rate=0.2):
        super().__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(
            input_size,
            hidden_size,
            num_layers,
            batch_first=True,
            bidirectional=False,
            dropout=dropout_rate if num_layers > 1 else 0.0,
        )
        self.cond2hidden = nn.Linear(cond_dim, hidden_size)
    def forward(self, x,context):
        batch_size = x.size(0)
        cond_hidden = self.cond2hidden(context)
        h0 = cond_hidden.unsqueeze(0).repeat(self.num_layers, 1, 1)  # [1, B, hidden_size]
        c0 = torch.zeros(self.num_layers, batch_size, self.hidden_size, device=x.device)

        outputs, (hn, cn) = self.lstm(x, (h0, c0))
        return (hn, cn)

class Decoder(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, num_layers=1, dropout_rate=0.2):
        super().__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(
            input_size,
            hidden_size,
            num_layers,
            batch_first=True,
            bidirectional=False,
            dropout=dropout_rate if num_layers > 1 else 0.0,
        )
        self.fc = nn.Linear(hidden_size, output_size)

    def forward(self, x, hidden):
        # x: tensor of shape (batch_size, seq_length, latent_size)
        output, (hid, cell) = self.lstm(x, hidden) #[B,52,hid]
        prediction = self.fc(output)#[B,52,2]
        return prediction,(hid,cell)

class LSTMVAE(nn.Module):
    """LSTM-based Variational Auto Encoder"""

    def __init__(
        self, input_size, hidden_size, latent_size, output_size,dropout_rate=0.2,device=torch.device("cuda")
    ):
        super(LSTMVAE, self).__init__()
        self.device = device

        # dimensions
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.latent_size = latent_size
        self.num_layers = 2


        # lstm ae
        self.lstm_enc = Encoder(
            input_size=input_size, hidden_size=hidden_size, num_layers=self.num_layers,dropout_rate=dropout_rate
        )
        self.lstm_dec = Decoder(
            input_size=latent_size,
            hidden_size=hidden_size,
            output_size=output_size,
            num_layers=self.num_layers,
            dropout_rate=dropout_rate,
        )

        # Variational components
        self.mu = nn.Linear(self.hidden_size, self.latent_size)
        self.var = nn.Linear(self.hidden_size, self.latent_size)
        self.fc3 = nn.Linear(self.latent_size, self.hidden_size)
        self.dropout = nn.Dropout(p=0.2)
      
    def reparametize(self, mean, logvar):
        std = torch.exp(0.5 * logvar)
        noise = torch.randn_like(std).to(self.device)
        z = mean + noise * std
        return z

    def forward(self, x, context):

        batch_size, seq_len, _ = x.shape
        enc_hidden = self.lstm_enc(x,context) #([1,B,hidden],[1,B,hidden])
        enc_h = enc_hidden[0][-1].view(batch_size, self.hidden_size).to(self.device)#[B, hidden_layer:256]
        
        mean = self.mu(enc_h) #[B,latent:128]
        logvar = self.var(enc_h)

        z = self.reparametize(mean,logvar)#[B,128]

        h_ = self.fc3(z) #[B,hidden:128]
        h_ = self.dropout(h_)
        z = z.unsqueeze(1)
        z = z.repeat(1,seq_len,1) #[B,52,latent:128]
        h0_dec = h_.unsqueeze(0).repeat(self.num_layers, 1, 1)
        c0_dec = torch.zeros(self.num_layers, batch_size, self.hidden_size, device=x.device)
        hidden = (h0_dec, c0_dec)
        reconstruct_output, hidden = self.lstm_dec(z, hidden)

        return reconstruct_output,mean,logvar

    def getZ(self,x, context):
        batch_size, seq_len, feature_dim = x.shape
        enc_hidden = self.lstm_enc(x,context) #([1,B,hidden],[1,B,hidden])
        enc_h = enc_hidden[0].view(batch_size, self.hidden_size).to(self.device)#[B, hidden_layer]
        
        mean = self.mu(enc_h) #[B,latent:128]
        logvar = self.var(enc_h)

        z = self.reparametize(mean,logvar)#[B,128]

        return z
    def getTraj(self,z,num_samp):
        h_ = self.fc3(z)#[B,256]
        z = z.unsqueeze(1)
        z = z.repeat(1,52,1) #[B,128]->[B,1,128]
        #z = z.view(128*num_samp,52,self.latent_size).to(self.device)
        hidden = (h_.unsqueeze(0).contiguous(), h_.unsqueeze(0).contiguous())#([1,B,hid],[1,B,hid])
        reconstruct_output, _ = self.lstm_dec(z, hidden)
        return reconstruct_output
    def loss_function(self, *args, **kwargs) -> dict:
        """
        Computes the VAE loss function.
        KL(N(\mu, \sigma), N(0, 1)) = \log \frac{1}{\sigma} + \frac{\sigma^2 + \mu^2}{2} - \frac{1}{2}
        """
        recons = args[0]
        input = args[1]
        mu = args[2]
        log_var = args[3]

        kld_weight = args[4]
        recons_loss = F.mse_loss(recons, input)

        kld_loss = torch.mean( -0.5 * torch.sum(1 + log_var - mu**2 - log_var.exp(), dim=1) )

   
        loss = recons_loss + kld_weight * kld_loss
        return {
            "loss": loss,
            "Reconstruction_Loss": recons_loss,
            "KLD": kld_loss,
        }

    