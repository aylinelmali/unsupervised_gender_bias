from __future__ import print_function

import argparse
import os, sys
import time
import numpy as np
import torch
import torch.nn as nn
import copy
import codecs
import random
import pdb
pdb.set_trace()

from datasets import make_rt_gender, make_rt_gender_op_posts
from model import *
from torchtext.vocab import GloVe
from tqdm import tqdm

def make_parser():
  parser = argparse.ArgumentParser(description='PyTorch RNN Classifier w/ attention')
  parser.add_argument('--data', type=str, default='SST', choices=["RT_GENDER",
                                                                  "RT_GENDER_OP_POSTS"
                                                                ],
                        help='Data corpus: [RT_GENDER or RT_GENDER_OP_POSTS]')
  parser.add_argument('--base_path', type=str, required=True,
                      help='path of base folder')

  parser.add_argument('--test_file', type=str, default="",
                      help='name of test file')
  parser.add_argument('--ood_test_file', type=str, default="",
                      help='name of test file')
  parser.add_argument('--train_file', type=str, default="",
                      help='name of train file')
  parser.add_argument('--valid_file', type=str, default="",
                      help='name of valid file')

  parser.add_argument('--rnn_model', type=str, default='LSTM',
                        help='type of recurrent net [LSTM, GRU]')
  parser.add_argument('--save_dir', type=str,
                        help='Directory to save the model')
  parser.add_argument('--model', type=str,
                        help='CNN or RNN or FFN (uses topics as features)')
  parser.add_argument('--model_name', type=str,
                        help='Model name to save')
  parser.add_argument('--topic_loss', type=str, default="ce",
                        help='in [mse|ce]')
  parser.add_argument('--emsize', type=int, default=128,
                        help='size of word embeddings [Uses pretrained on 50, 100, 200, 300]')
  parser.add_argument('--hidden', type=int, default=128,
                        help='number of hidden units for the RNN encoder')
  parser.add_argument('--nlayers', type=int, default=1,
                        help='number of layers of the RNN encoder')
  parser.add_argument('--num_topics', type=int, default=50,
                        help='Number of Topics')
  parser.add_argument('--lr', type=float, default=1e-4,
                        help='initial learning rate')
  parser.add_argument('--clip', type=float, default=5,
                        help='gradient clipping')
  parser.add_argument('--epochs', type=int, default=5,
                        help='upper epoch limit')
  parser.add_argument('--gpu', type=int, default=0,
                        help='which gpu to use')
  parser.add_argument('--alpha', type=float, default=0.001,
                        help='coefficient for reverse gradient')
  parser.add_argument('--batch_size', type=int, default=256, metavar='N',
                        help='batch size')
  parser.add_argument('--drop', type=float, default=0,
                        help='dropout')
  parser.add_argument('--gradreverse', action='store_false',
                        help='Reverse Gradients if not set')
  parser.add_argument('--bi', action='store_false',
                        help='[DON\'T USE] bidirectional encoder')
  parser.add_argument('--save_output_topics', action='store_true',
                        help='save output topics in file')
  parser.add_argument('--output_topics_save_filename', type=str,
                        help='where to save output topics')
  parser.add_argument('--cuda', action='store_false',
                    help='[DONT] use CUDA')
  parser.add_argument('--load', action='store_true',
                    help='Load and Evaluate the model on test data, dont train')
  parser.add_argument('--latest', action='store_true',
                    help='Load and Evaluate the model on test data, dont train')
  parser.add_argument('--write_attention', action='store_true',
                    help='write attention values to file')
  parser.add_argument('--write_predictions', action='store_true',
                    help='write attention values to file')
  parser.add_argument('--demote_topics', action='store_true',
                    help='[Demote] topics adversarially while training')
  parser.add_argument('--onlytopics', action='store_true',
                    help='[Demote] topics adversarially while training')

  parser.add_argument('--alpha_sched', action='store_true',help="use a schedule on alpha")
  parser.add_argument('--alpha_sched2', action='store_true',help="use a schedule on alpha")

  parser.add_argument('-kernel-num', type=int, default=100, help='number of each kind of kernel')
  parser.add_argument('-kernel-sizes', type=str, default='3,4,5', help='comma-separated kernel size to use for convolution')

  return parser


topic_criterion = nn.KLDivLoss(size_average=False)
# topic_criterion = nn.CrossEntropyLoss()
def seed_everything(seed, cuda=False):
  # Set the random seed manually for reproducibility.
  np.random.seed(seed)
  random.seed(seed)
  torch.manual_seed(seed)
  if cuda:
    torch.cuda.manual_seed_all(seed)


def update_stats(accuracy, confusion_matrix, logits, y):
  _, max_ind = torch.max(logits, 1)
  equal = torch.eq(max_ind, y)
  correct = int(torch.sum(equal))

  for j, i in tqdm(zip(max_ind, y)):
    confusion_matrix[int(i),int(j)]+=1

  return accuracy + correct, confusion_matrix

def update_stats_topics(accuracy, confusion_matrix, logits, y):
  _, max_ind = torch.max(logits, 1)
  _, max_ind_y = torch.max(y, 1)
  equal = torch.eq(max_ind, max_ind_y)
  correct = int(torch.sum(equal))

  for j, i in tqdm(zip(max_ind, max_ind_y)):
    confusion_matrix[int(i),int(j)]+=1

  return accuracy + correct, confusion_matrix


def train(model, data, optimizer, criterion, args, epoch):
  model.train()
  accuracy, confusion_matrix = 0.0, np.zeros((args.nlabels, args.nlabels), dtype=int)
  accuracy_fromtopics, confusion_matrix_ = 0.0, np.zeros((args.num_topics, args.num_topics), dtype=int)
  t = time.time()
  total_loss = 0
  total_topic_loss = 0
  num_batches = len(data)
  for batch_num, batch in tqdm(enumerate(data)):

    if args.alpha_sched2:
      p = (batch_num + epoch * num_batches)/(args.epochs * num_batches)
      alpha = 2/(1+np.exp(-10*p)) - 1
    elif args.alpha_sched:
      if epoch < 3:
        alpha = -1
      else:
        alpha = args.alpha
    else:
      alpha = args.alpha

    model.zero_grad()
    x, lens = batch.text
    y = batch.label
    padding_mask = x.ne(1).float()

    if args.model == "FFN":
      topics = batch.topics
      logits, _, topic_logprobs = model(topics)
    else:
      logits, energy, topic_logprobs, _ = model(x, gradreverse=args.gradreverse, alpha=alpha, padding_mask=padding_mask)
      if energy is not None:
        energy = torch.squeeze(energy)
    # print (x.size())
    # print (energy.size())
    # print (energy)
    # input()
    loss = criterion(logits.view(-1, args.nlabels), y)
    if torch.isnan(loss):
      print ()
      print ("something has become nan")
      print(logits)
      print (y)
      print (x)
      print (lens)
      # input("Press Ctrl+C")
      continue
    total_loss += float(loss)

    if args.demote_topics:
      # topics = batch.topics
      topics = batch.topics
      if args.topic_loss == "ce":
        # g = topic_logprobs * topics
        # topic_loss = -g.sum(dim=-1).mean()
        topic_loss = topic_criterion(topic_logprobs, topics)
      else:
        g = (topics - torch.exp(topic_logprobs))
        topic_loss = (g*g).sum(dim=-1).mean()
      if args.onlytopics:
        loss = topic_loss
      else:
        loss += topic_loss
      total_topic_loss += float(topic_loss)

    accuracy, confusion_matrix = update_stats(accuracy, confusion_matrix, logits, y)
    if args.demote_topics:
      accuracy_fromtopics, confusion_matrix_ = update_stats_topics(accuracy_fromtopics, confusion_matrix_, topic_logprobs, topics)
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
    optimizer.step()

    print("[Batch]: {}/{} in {:.5f} seconds".format(
          batch_num, len(data), time.time() - t), end='\r')
    t = time.time()

  print()
  print("[Topic Loss]: {:.5f}".format(total_topic_loss / len(data)))
  print("[Loss]: {:.5f}".format(total_loss / len(data)))
  print("[Accuracy]: {}/{} : {:.3f}%".format(
        accuracy, len(data.dataset), accuracy / len(data.dataset) * 100))
  print("[accuracy_fromtopics]: {}/{} : {:.3f}%".format(
        accuracy_fromtopics, len(data.dataset), accuracy_fromtopics / len(data.dataset) * 100))
  print(confusion_matrix)
  return total_loss / len(data)


def evaluate(model, data, optimizer, criterion, args, datatype='Valid', writetopics=False, itos=None, litos=None):
  model.eval()
  extra_str = ""
  if args.latest:
    extra_str = "_latest"
  if writetopics:
    topicfile = open(args.save_dir+"/"+datatype+"_"+args.output_topics_save_filename+".txt","w")
  if (args.write_attention or args.write_predictions) and itos is not None:
    attention_file = codecs.open(args.save_dir+"/"+datatype+"."+args.model_name+"_attention"+extra_str+".txt", "w", encoding="utf8")
  accuracy, confusion_matrix = 0.0, np.zeros((args.nlabels, args.nlabels), dtype=int)
  t = time.time()
  total_loss = 0
  total_topic_loss = 0
  with torch.no_grad():
    for batch_num, batch in tqdm(enumerate(data)):
      # print (len(batch.text))
      x, lens = batch.text
      y = batch.label
      if args.data in ["RT_GENDER", "RT_GENDER_OP_POSTS"]:
        indices = batch.index.cpu().data.numpy()
        # print (indices.size())
      else:
        indices = np.array(([0]*len(y)))
      padding_mask = x.ne(1).float()
      # topics = batch.topics

      if args.model == "FFN":
        logits, _, _ = model(topics)
      else:
        logits, energy, topic_logprobs, _ = model(x, gradreverse=args.gradreverse, padding_mask=padding_mask)

      # if args.demote_topics and datatype=="train":
      #   topics = batch.topics
      #   topic_loss = -(topic_logprobs * topics).sum(dim=-1).mean()
      #   # loss = topic_loss
      #   total_topic_loss += float(topic_loss)

      if writetopics:
        for topiclogprob in topic_logprobs.cpu().data.numpy():
          topicfile.write(" ".join([str(np.exp(t)) for t in topiclogprob])+"\n")

      if args.write_attention and itos is not None:
        m = torch.nn.Softmax()
        soft_logits = m(logits)
        max_val, max_ind = torch.max(soft_logits, 1)
        first_val = soft_logits[:, 0]
        energy = energy.squeeze(1).cpu().data.numpy()
        for sentence, length, attns, ll, mi, index, max_val, first_val in tqdm(zip(x.permute(1,0).cpu().data.numpy(), lens.cpu().data.numpy(), energy, y.cpu().data.numpy(), max_ind.cpu().data.numpy(), indices, max_val.cpu().data.numpy(), first_val.cpu().data.numpy())):
          s = ""
          for wordid, attn in tqdm(zip(sentence[:length], attns[:length])):
            s += str(itos[wordid])+":"+str(attn)+" "
          gold = str(litos[ll])
          pred = str(litos[mi])
          # print (index)
          index = str(str(index))
          max_val = str(max_val)
          z = s+"\t"+gold+"\t"+pred+"\t"+index+"\t" + max_val + "\t" + str(first_val) + "\n"
          attention_file.write(z)

      if args.write_predictions and itos is not None:
        m = torch.nn.Softmax()
        soft_logits = m(logits)
        max_val, max_ind = torch.max(soft_logits, 1)
        for sentence, length, ll, mi, index, max_val in tqdm(zip(x.permute(1,0).cpu().data.numpy(), lens.cpu().data.numpy(), y.cpu().data.numpy(), max_ind.cpu().data.numpy(), indices, max_val.cpu().data.numpy())):
          s = ""
          for wordid in tqdm(sentence[:length]):
            s += str(itos[wordid])+" "
          gold = str(litos[ll])
          pred = str(litos[mi])
          # print (index)
          index = str(index)
          max_val = str(max_val)
          z = s+"\t"+gold+"\t"+pred+ "\t" + index + "\t" + max_val + "\n"
          attention_file.write(z)

      bloss = criterion(logits.view(-1, args.nlabels), y)
      if torch.isnan(bloss):
        print ("NANANANANANA")
        print (logits)
        print (y)
        print (x)
        input("Press Ctrl+C")
      total_loss += float(bloss)
      accuracy, confusion_matrix = update_stats(accuracy, confusion_matrix, logits, y)
      print("[Batch]: {}/{} in {:.5f} seconds".format(
            batch_num, len(data), time.time() - t), end='\r')
      t = time.time()
  if writetopics:
    topicfile.close()
  if (args.write_attention or args.write_predictions) and itos is not None:
    attention_file.close()

  print()
  print("[{} topic loss]: {:.5f}".format(datatype, total_topic_loss / len(data)))
  print("[{} loss]: {:.5f}".format(datatype, total_loss / len(data)))
  print("[{} accuracy]: {}/{} : {:.3f}%".format(datatype,
        accuracy, len(data.dataset), accuracy / len(data.dataset) * 100))
  print(confusion_matrix)
  return total_loss / len(data)

pretrained_GloVe_sizes = [50, 100, 200, 300]

def load_pretrained_vectors(dim):
  if dim in pretrained_GloVe_sizes:
    # Check torchtext.datasets.vocab line #383
    # for other pretrained vectors. 6B used here
    # for simplicity
    name = 'glove.{}.{}d'.format('6B', str(dim))
    return name
  return None

def main():
  args = make_parser().parse_args()
  print("[Model hyperparams]: {}".format(str(args)))

  cuda = torch.cuda.is_available() and args.cuda
  device = torch.device("cpu") if not cuda else torch.device("cuda:"+str(args.gpu))
  seed_everything(seed=1337, cuda=cuda)
  vectors = None #don't use pretrained vectors
  # vectors = load_pretrained_vectors(args.emsize)

  # Load dataset iterators
  if args.data == "RT_GENDER":
    iters, TEXT, LABEL, INDEX = make_rt_gender(args.batch_size, base_path=args.base_path, train_file=args.train_file, valid_file=args.valid_file, test_file=args.test_file, device=device, vectors=vectors, topics=False)
    train_iter, val_iter, test_iter = iters
  elif args.data == "RT_GENDER_OP_POSTS":
    iters, TEXT, LABEL, INDEX = make_rt_gender_op_posts(args.batch_size, base_path=args.base_path, train_file=args.train_file, valid_file=args.valid_file, test_file=args.test_file, device=device, vectors=vectors)
    if len(iters) == 2:
      train_iter, test_iter = iters
      val_iter = test_iter
    else:
      train_iter, val_iter, test_iter = iters
  else:
    assert False

  print("[Corpus]: train: {}, test: {}, vocab: {}, labels: {}".format(
            len(train_iter.dataset), len(test_iter.dataset), len(TEXT.vocab), len(LABEL.vocab)))

  if args.model == "CNN":
    args.embed_num = len(TEXT.vocab)
    args.nlabels = len(LABEL.vocab)
    args.kernel_sizes = [int(k) for k in args.kernel_sizes.split(',')]
    args.embed_dim = args.emsize

    model = CNN_Text(args, num_topics=args.num_topics)

  elif args.model == "FFN_BOW":
    args.embed_num = len(TEXT.vocab)
    args.nlabels = len(LABEL.vocab)
    args.embed_dim = args.emsize

    model = FFN_BOW_Text(args)

  elif args.model == "FFN":
    args.nlabels = len(LABEL.vocab)
    model = FFN_Text(args)

  else:
    ntokens, nlabels = len(TEXT.vocab), len(LABEL.vocab)
    args.nlabels = nlabels # hack to not clutter function arguments

    embedding = nn.Embedding(ntokens, args.emsize, padding_idx=1)
    if vectors: embedding.weight.data.copy_(TEXT.vocab.vectors)
    encoder = Encoder(args.emsize, args.hidden, nlayers=args.nlayers,
                      dropout=args.drop, bidirectional=args.bi, rnn_type=args.rnn_model)

    attention_dim = args.hidden if not args.bi else 2*args.hidden
    attention = BahdanauAttention(attention_dim, attention_dim)

    model = Classifier(embedding, encoder, attention, attention_dim, nlabels, num_topics=args.num_topics)

  model.to(device)

  criterion = nn.CrossEntropyLoss()
  # topic_criterion = nn.KLDivLoss(size_average=False)
  topic_criterion = nn.CrossEntropyLoss()
  optimizer = torch.optim.Adam(model.parameters(), args.lr, amsgrad=True)

  for p in tqdm(model.parameters()):
    if not p.requires_grad:
      print ("OMG", p)
      p.requires_grad = True
    p.data.uniform_(-0.5, 0.5)
    # print (p.data.norm())

  # trainloss = evaluate(best_model, train_iter, optimizer, criterion, args, datatype='train', writetopics=args.save_output_topics, itos=TEXT.vocab.itos)
  if args.load:
    if args.latest:
      best_model = torch.load(args.save_dir+"/"+args.model_name+"_latestmodel")
    else:
      best_model = torch.load(args.save_dir+"/"+args.model_name+"_bestmodel")
  else:
    try:
      best_valid_loss = None
      best_model = None
      for epoch in tqdm(range(1, args.epochs + 1)):
        train(model, train_iter, optimizer, criterion, args, epoch)
        loss = evaluate(model, val_iter, optimizer, criterion, args)

        if not best_valid_loss or loss < best_valid_loss:
          best_valid_loss = loss
          print ("Updating best model")
          best_model = copy.deepcopy(model)
          torch.save(best_model, args.save_dir+"/"+args.model_name+"_bestmodel")
        torch.save(model, args.save_dir+"/"+args.model_name+"_latestmodel")
    except KeyboardInterrupt:
      print("[Ctrl+C] Training stopped!")

  if not args.load:
    trainloss = evaluate(best_model, train_iter, optimizer, criterion, args, datatype='train', writetopics=args.save_output_topics, itos=TEXT.vocab.itos, litos=LABEL.vocab.itos)
    valloss = evaluate(best_model, val_iter, optimizer, criterion, args, datatype='valid', writetopics=args.save_output_topics, itos=TEXT.vocab.itos, litos=LABEL.vocab.itos)
  loss = evaluate(best_model, test_iter, optimizer, criterion, args, datatype=os.path.basename(args.test_file).replace(".txt", "").replace(".tsv", ""), writetopics=args.save_output_topics, itos=TEXT.vocab.itos, litos=LABEL.vocab.itos)


if __name__ == '__main__':
  main()
