import argparse
import random
import warnings
import time

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from sklearn.metrics import precision_recall_fscore_support, f1_score
from tqdm import tqdm

from dataloader import DataGenerator
from model import Model

parser = argparse.ArgumentParser()
# fine-tuning setting
parser.add_argument('--pretrained_log_name', type=str,
                    default='BGL_2k', help='log file name')
parser.add_argument("--load_path", type=str,
                    default='checkpoints/train_BGL_2k_classifier_1_64_1e-05-best.pt', help="latest model path")
parser.add_argument('--log_name', type=str,
                    default='BGL_2k', help='log file name')
parser.add_argument('--tune_mode', type=str, default='adapter',
                    help='tune adapter or classifier only')
# model setting
parser.add_argument('--num_layers', type=int, default=1,
                    help='num of encoder layer')
parser.add_argument('--lr', type=float, default=1e-5)
parser.add_argument('--window_size', type=int,
                    default='20', help='log sequence length')
parser.add_argument('--adapter_size', type=int, default=64,
                    help='adapter size')
parser.add_argument('--epoch', type=int, default=20,
                    help='epoch')
args = parser.parse_args()
suffix = f'{args.log_name}_from_{args.pretrained_log_name}_{args.tune_mode}_{args.num_layers}_{args.adapter_size}_{args.lr}_{args.epoch}'

with open(f'result/tune_{suffix}.txt', 'w', encoding='utf-8') as f:
    f.write(str(args)+'\n')

# hyper-parameters
EMBEDDING_DIM = 768
batch_size = 64
epochs = args.epoch
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
device_ids = [0, 1]

# fix all random seeds
warnings.filterwarnings('ignore')
torch.manual_seed(123)
torch.cuda.manual_seed(123)
np.random.seed(123)
random.seed(123)
torch.backends.cudnn.deterministic = True
# torch.backends.cudnn.benchmark = True

# load data Hdfs
training_data = np.load(
    f'./preprocessed_data/{args.log_name}_training_data.npz', allow_pickle=True)
# load test data Hdfs
testing_data = np.load(
    f'./preprocessed_data/{args.log_name}_testing_data.npz', allow_pickle=True)
x_train, y_train = training_data['x'], training_data['y']
x_test, y_test = testing_data['x'], testing_data['y']
del testing_data
del training_data

train_generator = DataGenerator(x_train, y_train, args.window_size)
test_generator = DataGenerator(x_test, y_test, args.window_size)
train_loader = torch.utils.data.DataLoader(
    train_generator, batch_size=batch_size, shuffle=True)
test_loader = torch.utils.data.DataLoader(
    test_generator, batch_size=batch_size, shuffle=False)

# load pretrained model
model = Model(mode='adapter', num_layers=args.num_layers, adapter_size=args.adapter_size, dim=EMBEDDING_DIM, window_size=args.window_size, nhead=8, dim_feedforward=4 *
              EMBEDDING_DIM, dropout=0.1)
# fine tuning setting
if args.tune_mode == 'adapter':
    model.train_adapter()
elif args.tune_mode == 'classifier':
    model.train_classifier()
elif args.tune_mode == 'tuning':
    for param in model.parameters():
        param.requires_grad = True

model = model.to(device)
model = torch.nn.DataParallel(model, device_ids=device_ids)


if args.pretrained_log_name != 'random':
    checkpoint = torch.load(args.load_path)
    net = checkpoint['net']
    net.pop('module.fc1.weight')
    net.pop('module.fc1.bias')
    r = model.load_state_dict(net, strict=False)
    with open(f'result/tune_{suffix}.txt', 'a', encoding='utf-8') as f:
        f.write(f'loading pretrained model {args.load_path}\n')
        f.write(f'loading result: {r}\n')


optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=0)
scheduler = optim.lr_scheduler.OneCycleLR(
    optimizer, max_lr=args.lr, epochs=epochs, steps_per_epoch=len(train_loader))
criterion = nn.BCEWithLogitsLoss()

best_f1 = 0
start_epoch = -1
log_interval = 100
for epoch in range(start_epoch+1, epochs):
    loss_all, f1_all = [], []
    train_loss = 0
    train_pred, train_true = [], []

    model.train()
    start_time = time.time()
    for batch_idx, data in enumerate(tqdm(train_loader)):
        x, y = data[0].to(device), data[1].to(device)
        x = x.to(torch.float32)
        y = y.to(torch.float32)
        out = model(x)
        loss = criterion(out, y)

        optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 0.5)
        optimizer.step()
        scheduler.step()

        train_loss += loss.item()
        train_pred.extend(out.argmax(1).tolist())
        train_true.extend(y.argmax(1).tolist())

        if batch_idx % log_interval == 0 and batch_idx > 0:
            time_cost = time.time()-start_time
            cur_loss = train_loss / log_interval
            # scheduler.step(cur_loss)
            cur_f1 = f1_score(train_true, train_pred)

            with open(f'result/tune_{suffix}.txt', 'a', encoding='utf-8') as f:
                f.write(f'| epoch {epoch:3d} | {batch_idx:5d}/{len(train_loader):5d} batches | '
                        f'loss {cur_loss:2.5f} |'
                        f'f1 {cur_f1:.5f} |'
                        f'time {time_cost:4.2f} |'
                        f'lr {scheduler.get_last_lr()}\n')
            print(f'| epoch {epoch:3d} | {batch_idx:5d}/{len(train_loader):5d} batches | '
                  f'loss {cur_loss} |'
                  f'f1 {cur_f1}',
                  f'lr {scheduler.get_last_lr()}')

            loss_all.append(train_loss)
            f1_all.append(cur_f1)
            train_loss = 0
            train_acc = 0
            start_time = time.time()

    train_loss = sum(loss_all) / len(train_loader)
    print("epoch : {}/{}, loss = {:.6f}".format(epoch, epochs, train_loss))

    model.eval()
    n = 0.0
    with torch.no_grad():
        for batch_idx, data in enumerate(tqdm(test_loader)):
            x, y = data[0].to(device), data[1].to(device)
            x = x.to(torch.float32)
            y = y.to(torch.float32)
            out = model(x).cpu()
            if batch_idx == 0:
                y_pred = out
                y_true = y.cpu()
            else:
                y_pred = np.concatenate((y_pred, out), axis=0)
                y_true = np.concatenate((y_true, y.cpu()), axis=0)

    # calculate metrics
    y_true = np.argmax(y_true, axis=1)
    y_pred = np.argmax(y_pred, axis=1)
    report = precision_recall_fscore_support(y_true, y_pred, average='binary')
    with open(f'result/tune_{suffix}.txt', 'a', encoding='utf-8') as f:
        f.write('number of epochs:'+str(epoch)+'\n')
        f.write('Number of testing data:'+str(x_test.shape[0])+'\n')
        f.write('Precision:'+str(report[0])+'\n')
        f.write('Recall:'+str(report[1])+'\n')
        f.write('F1 score:'+str(report[2])+'\n')
        f.write('all_loss:'+str(loss_all)+'\n')
        f.write('\n')
        f.close()

    print(f'Number of testing data: {x_test.shape[0]}')
    print(f'Precision: {report[0]:.4f}')
    print(f'Recall: {report[1]:.4f}')
    print(f'F1 score: {report[2]:.4f}')
