import torch
import os
from torch import nn, optim
from VAE import VAE
import numpy as np
import math
from torchvision.datasets import utils
import torch.utils.data as data_utils
import matplotlib.pyplot as plt
from torch.nn import functional as F
from scipy.special import logsumexp


def get_data_loader(dataset_location, batch_size):
    URL = "http://www.cs.toronto.edu/~larocheh/public/datasets/binarized_mnist/"
    # start processing

    def lines_to_np_array(lines):
        return np.array([[int(i) for i in line.split()] for line in lines])
    splitdata = []
    for splitname in ["train", "valid", "test"]:
        filename = "binarized_mnist_%s.amat" % splitname
        filepath = os.path.join(dataset_location, filename)
        # utils.download_url(URL + filename, dataset_location, filename=filename, md5=None)
        with open(filepath) as f:
            lines = f.readlines()
        x = lines_to_np_array(lines).astype('float32')
        x = x.reshape(x.shape[0], 1, 28, 28)
        # pytorch data loader
        dataset = data_utils.TensorDataset(torch.from_numpy(x))
        dataset_loader = data_utils.DataLoader(x, batch_size=batch_size, shuffle=splitname == "train")
        splitdata.append(dataset_loader)
    return splitdata


def train(model, optimizer, epoch, train_loader, device):
    model.train()
    train_loss = 0.0
    for batch_idx, inputs in enumerate(train_loader):
        inputs = inputs.to(device)
        optimizer.zero_grad()
        recon_output, mu, logvar = model(inputs)
        elbo_loss = model.ELBO(recon_output, inputs, mu, logvar)
        elbo_loss.backward()
        train_loss += elbo_loss.item()
        optimizer.step()

        if batch_idx % 10 == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(inputs), len(train_loader.dataset),
                100. * batch_idx / len(train_loader),
                elbo_loss.item() / len(inputs)))

    print('Train Epoch: {} Average loss: {:.4f}'.format(
          epoch, train_loss / len(train_loader.dataset)))
    return train_loss / len(train_loader.dataset)


def test(model, epoch, test_loader, device, split="Valid"):

    model.eval()
    test_loss = 0.0
    with torch.no_grad():
        for batch_idx, inputs in enumerate(test_loader):
            inputs = inputs.to(device)
            recon_output, mu, logvar = model(inputs)
            elbo_loss = model.ELBO(recon_output, inputs, mu, logvar)
            test_loss += elbo_loss.item()

            if batch_idx % 10 == 0:
                print('{} Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                    split, epoch, batch_idx * len(inputs), len(test_loader.dataset),
                    100. * batch_idx / len(test_loader),
                    elbo_loss.item() / len(inputs)))

    print('{} Epoch: {} Average loss: {:.4f}'.format(
          split, epoch, test_loss / len(test_loader.dataset)))
    return test_loss / len(test_loader.dataset)


def generate(model_path, device):
    print("Loading model....\n")
    model = VAE().to(device)
    model.load_state_dict(torch.load(model_path, map_location=device))
    model.eval()
    with torch.no_grad():
        z = torch.randn(10, 100).to(device)
        decoding = model.fc_decode(z)
        decoding = decoding.reshape(decoding.shape[0], decoding.shape[1], 1, 1)
        gen_output = model.decoder(decoding)
        gen_output = gen_output.squeeze().detach().cpu().numpy()
        gen_output[gen_output >= 0.5] = 1
        gen_output[gen_output < 0.5] = 0
    # plotting
    fig, axs = plt.subplots(2, 5)
    fig.suptitle('Generated images')

    for i in range(2):
        for j in range(5):
            axs[i, j].imshow(gen_output[j + i * 5])
    plt.savefig("Generated_Samples.png")


def generate_K_samples(mu, logvar, K):
    std = torch.exp(0.5 * logvar)
    e = torch.randn((mu.shape[0], K, mu.shape[1])).to(mu.device)
    Z = mu.unsqueeze(1) + e * std.unsqueeze(1)
    return Z


def importance_sampling(model, mini_batch_x, Z):
    """
    args:
        model: trained VAE model
        mini_batch_x: one mini batch of images either from validation set or test set 
            mini_batch_x of size(M, 1, 28, 28)
        Z: importance samples, Z of size (M, K, L)
    return:
        log_px: log-likelihood estimates of size (M,)
    """

    # The number of importance samples
    K = 200
    # calulate mu and logvar
    hidden = model.encoder(mini_batch_x)
    mu = model.fc_mu(hidden.squeeze())
    logvar = model.fc_logvar(hidden.squeeze())

    log_p_x_z = []
    # Loop over K importance samples
    for k in range(K):
        z = Z[:, k, :]
        decoding = model.fc_decode(z)
        decoding = decoding.reshape(decoding.shape[0], decoding.shape[1], 1, 1)
        recon_output = model.decoder(decoding)

        log_prob = -F.binary_cross_entropy_with_logits(recon_output, mini_batch_x, reduction='none')
        log_p_x_z.append(log_prob.squeeze().sum(2).sum(1))

    log_p_x_z = torch.stack(log_p_x_z, 0).transpose(1, 0)  # (M,K)

    var = logvar.exp()
    det_covar = var.prod(dim=1, keepdim=True).cpu().numpy()
    det_factor = np.sqrt(det_covar) / K

    p_z_q_z = log_p_x_z - 0.5 * (Z.pow(2) - (Z - mu.unsqueeze(1)).pow(2) / var.unsqueeze(1)).sum(2)
    p_z_q_z = p_z_q_z.cpu().numpy()
    log_px = logsumexp(p_z_q_z, axis=1, b=det_factor)
    return log_px


def estimate_log_likelihood(data_loader, device, split="valid"):

    model_path = "best_model.pth"
    model = VAE().to(device)
    model.load_state_dict(torch.load(model_path, map_location=device))
    # The number of importance samples
    K = 200
    model.eval()

    log_likelihood_estimate = 0.0

    with torch.no_grad():
        for batch_idx, mini_batch_x in enumerate(data_loader):
            mini_batch_x = mini_batch_x.to(device)
            recon_output, mu, logvar = model(mini_batch_x)

            # sample K=200 importance samples from posterior q(z|x_i)
            # Z is of size (M, K, L)
            Z = generate_K_samples(mu, logvar, K)
            # estimate log-likelihood for a batch
            log_px = importance_sampling(model, mini_batch_x, Z)
            log_likelihood_estimate += log_px.sum()
            print('log_px estimate of mini-batch {} of {} set: {:.4f}'.format(batch_idx, split, log_px.mean()))

        print('log_px estimate of {} set: {:.4f}'.format(split, log_likelihood_estimate / len(data_loader.dataset)))


def main(train_loader, valid_loader, test_loader, n_epochs, device, lr=3e-4):

    model = VAE().to(device)
    optimizer = optim.Adam(model.parameters(), lr=lr)

    train_losses = []
    valid_losses = []

    # Training VAE
    for epoch in range(n_epochs):
        train_loss = train(model, optimizer, epoch, train_loader, device)
        valid_loss = test(model, epoch, valid_loader, device, split="Valid")
        train_losses.append(train_loss)
        valid_losses.append(valid_loss)

    print("Saving the model")
    torch.save(model.state_dict(), "best_model.pth")
    plt.plot(train_losses, label="train loss")
    plt.plot(valid_losses, label="valid loss")
    plt.title("Learning curves")
    plt.xlabel("Epochs")
    plt.ylabel("Negative ELBO")
    plt.legend()
    plt.savefig("Learning_curves.png")

    print("Evaluation on test set----------")
    test_loss = test(model, epoch, test_loader, device, split="Test")

if __name__ == "__main__":

    # fix seed
    np.random.seed(1234)
    torch.manual_seed(1234)

    # hyper-parameters
    n_epochs = 20
    lr = 3e-4

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # load the dataset
    train_loader, valid_loader, test_loader = get_data_loader("binarized_mnist", 64)

    # training and validation
    main(train_loader, valid_loader, test_loader, n_epochs, device, lr)

    # generate some samples using trained model
    model_path = "best_model.pth"
    generate(model_path, device)

    # Estimate log likelihood of trained model on validation set
    estimate_log_likelihood(valid_loader, device, split="valid")

    # Estimate log likelihood of trained model on test set
    estimate_log_likelihood(test_loader, device, split="test")
